import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as fs from 'fs';
import * as path from 'path';
import * as toml from 'toml';
import { Template } from 'aws-cdk-lib/assertions';

import { RemediationAgentStack } from '../lib/stacks/remediation-agent-stack';

type CfnResource = {
  Properties?: Record<string, unknown>;
};

type IamStatement = {
  Action?: string | string[];
};

type PolicyDocument = {
  Statement?: IamStatement[];
};

type ContainerDefinition = {
  Command?: string[];
  Environment?: { Name?: string; Value?: unknown }[];
};

function synthesize(desiredCount: number): Template {
  const app = new cdk.App({ context: { ns: 'RcaAgentDev' } });

  // 프로덕션과 동일하게 Healthcare 서비스를 별도 스택에 두어, SG 인그레스 교차
  // 참조가 Healthcare → Remediation 한 방향으로만 흐르도록 한다.
  const netStack = new cdk.Stack(app, 'NetStack');
  const vpc = new ec2.Vpc(netStack, 'Vpc', { maxAzs: 2 });
  const topic = new sns.Topic(netStack, 'NotificationTopic');
  const sessionTable = new dynamodb.Table(netStack, 'SessionTable', {
    partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
    sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
  });

  const hcStack = new cdk.Stack(app, 'HcStack');
  const cluster = new ecs.Cluster(hcStack, 'HcCluster', { vpc });
  const taskDef = new ecs.FargateTaskDefinition(hcStack, 'HcTask');
  taskDef.addContainer('hc', {
    image: ecs.ContainerImage.fromRegistry('placeholder'),
    portMappings: [{ containerPort: 8000 }],
  });
  const healthcareService = new ecs.FargateService(hcStack, 'HcService', {
    cluster,
    taskDefinition: taskDef,
  });

  const stack = new RemediationAgentStack(app, 'RemediationTest', {
    vpc,
    notificationTopic: topic,
    healthcareService,
    healthcareServiceHost: 'healthcare.rcaagentdev.local',
    rcaSessionTable: sessionTable,
    imageTag: 'latest',
    desiredCount,
  });
  return Template.fromStack(stack);
}

test('remediation queue subscribes only to rca_complete events to avoid loops', () => {
  const template = synthesize(1);

  template.hasResourceProperties('AWS::SNS::Subscription', {
    Protocol: 'sqs',
    FilterPolicy: {
      event_type: ['rca_complete'],
    },
  });
});

test('remediation queue has a dead-letter queue with redrive', () => {
  const template = synthesize(1);

  const queues = Object.values(
    template.findResources('AWS::SQS::Queue'),
  ) as CfnResource[];
  const queue = queues.find(
    (resource) =>
      resource.Properties?.QueueName === 'RcaAgentDevRemediationQueue',
  );
  const deadLetterQueue = queues.find(
    (resource) =>
      resource.Properties?.QueueName === 'RcaAgentDevRemediationDLQ',
  );

  expect(queue?.Properties).toEqual(
    expect.objectContaining({
      MessageRetentionPeriod: 4 * 24 * 60 * 60,
      VisibilityTimeout: 15 * 60,
      RedrivePolicy: expect.objectContaining({
        deadLetterTargetArn: expect.any(Object),
        maxReceiveCount: 3,
      }),
    }),
  );
  expect(deadLetterQueue?.Properties).toEqual(
    expect.objectContaining({
      MessageRetentionPeriod: 14 * 24 * 60 * 60,
      VisibilityTimeout: 10 * 60,
    }),
  );
});

test('task runs the remediation entrypoint with only the Healthcare reset target', () => {
  const template = synthesize(1);
  const taskDefinitions = Object.values(
    template.findResources('AWS::ECS::TaskDefinition'),
  ) as CfnResource[];
  const containers = taskDefinitions.flatMap(
    (resource) =>
      (resource.Properties?.ContainerDefinitions as
        | ContainerDefinition[]
        | undefined) ?? [],
  );
  const remediation = containers.find(
    (container) =>
      JSON.stringify(container.Command) ===
      JSON.stringify(['python', '-m', 'rca_agent.remediation_main']),
  );
  const environment = remediation?.Environment ?? [];

  expect(environment).toEqual(
    expect.arrayContaining([
      expect.objectContaining({
        Name: 'DYNAMODB_TABLE_NAME',
        Value: expect.any(Object),
      }),
      {
        Name: 'HEALTHCARE_SERVICE_HOST',
        Value: 'healthcare.rcaagentdev.local',
      },
    ]),
  );

  const serializedTemplate = JSON.stringify(template.toJSON());
  expect(serializedTemplate).not.toContain('ECS_CLUSTER_NAME');
  expect(serializedTemplate).not.toContain('ECS_SERVICE_NAME');
});

test('task can read, query, and update only the RCA session table', () => {
  const template = synthesize(1);
  const policies = Object.values(
    template.findResources('AWS::IAM::Policy'),
  ) as CfnResource[];
  const dynamodbStatements = policies
    .flatMap(
      (resource) =>
        (resource.Properties?.PolicyDocument as PolicyDocument | undefined)
          ?.Statement ?? [],
    )
    .filter((statement) =>
      (Array.isArray(statement.Action)
        ? statement.Action
        : [statement.Action]
      ).some(
        (action) =>
          typeof action === 'string' && action.startsWith('dynamodb:'),
      ),
    );

  const dynamodbActions = dynamodbStatements.flatMap((statement) =>
    Array.isArray(statement.Action) ? statement.Action : [statement.Action],
  );

  expect(dynamodbStatements).not.toEqual([]);
  expect(dynamodbActions).toEqual(
    expect.arrayContaining([
      'dynamodb:GetItem',
      'dynamodb:Query',
      'dynamodb:UpdateItem',
    ]),
  );
  for (const statement of dynamodbStatements) {
    const serializedStatement = JSON.stringify(statement);
    expect(serializedStatement).toContain('"Fn::ImportValue"');
    expect(serializedStatement).not.toContain('"Resource":"*"');
  }
});

test('desiredCount 0 leaves the feature flag off (analysis-only)', () => {
  const template = synthesize(0);

  template.hasResourceProperties('AWS::ECS::Service', {
    DesiredCount: 0,
  });
  expect(
    Object.values(template.findResources('AWS::SNS::Subscription')),
  ).toHaveLength(0);
});

test('dev keeps the separate Strands remediation service off by default', () => {
  const config = toml.parse(
    fs.readFileSync(path.resolve(__dirname, '../config/dev.toml'), 'utf-8'),
  ) as { remediation?: { desiredCount?: number } };

  expect(config.remediation?.desiredCount).toBe(0);
});

test('remediation task role cannot inspect or force an ECS deployment', () => {
  const template = synthesize(1);
  const policies = Object.values(
    template.findResources('AWS::IAM::Policy'),
  ) as CfnResource[];
  const statements = policies.flatMap(
    (resource) =>
      (resource.Properties?.PolicyDocument as PolicyDocument | undefined)
        ?.Statement ?? [],
  );
  const ecsActions = statements
    .flatMap((statement) =>
      Array.isArray(statement.Action) ? statement.Action : [statement.Action],
    )
    .filter(
      (action): action is string =>
        typeof action === 'string' && action.startsWith('ecs:'),
    );

  expect(ecsActions).toEqual([]);
});
