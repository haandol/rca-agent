import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as sns from 'aws-cdk-lib/aws-sns';
import { Match, Template } from 'aws-cdk-lib/assertions';

import { HeadlessCodexStack } from '../lib/stacks/headless-codex-stack';

type CfnResource = {
  Properties?: Record<string, unknown>;
};

type IamStatement = {
  Effect?: string;
  Action?: string | string[];
  Resource?: unknown;
};

type PolicyDocument = {
  Statement?: IamStatement[];
};

type TaskRoleArn = {
  'Fn::GetAtt'?: [string, string];
};

type SynthesizedTemplates = {
  app: cdk.App;
  headlessCodexStack: HeadlessCodexStack;
  headlessCodex: Template;
  healthcare: Template;
};

function synthesize(): SynthesizedTemplates {
  const app = new cdk.App({ context: { ns: 'RcaAgentDev' } });
  const dependencies = new cdk.Stack(app, 'Dependencies');
  const vpc = new ec2.Vpc(dependencies, 'Vpc', { maxAzs: 2 });
  const alarmTopic = new sns.Topic(dependencies, 'AlarmTopic');
  const notificationTopic = new sns.Topic(dependencies, 'NotificationTopic');
  const sessionTable = new dynamodb.Table(dependencies, 'SessionTable', {
    partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
  });
  const evidenceBucket = new s3.Bucket(dependencies, 'EvidenceBucket');

  // Healthcare is synthesized so the ingress assertion can prove that analysis
  // opens no route to the service it investigates.
  const healthcareStack = new cdk.Stack(app, 'Healthcare');
  const healthcareCluster = new ecs.Cluster(
    healthcareStack,
    'HealthcareCluster',
    { vpc },
  );
  const healthcareTaskDefinition = new ecs.FargateTaskDefinition(
    healthcareStack,
    'HealthcareTaskDefinition',
  );
  healthcareTaskDefinition.addContainer('Healthcare', {
    image: ecs.ContainerImage.fromRegistry('placeholder'),
    portMappings: [{ containerPort: 8000 }],
  });
  new ecs.FargateService(healthcareStack, 'HealthcareService', {
    cluster: healthcareCluster,
    taskDefinition: healthcareTaskDefinition,
  });

  const stack = new HeadlessCodexStack(app, 'CcHeadlessTest', {
    vpc,
    alarmTopic,
    notificationTopic,
    rcaSessionTable: sessionTable,
    evidenceBucket,
    vectorBucketName: 'rca-test-vectors',
    reportBucket: 'rca-test-reports',
    imageTag: 'latest',
  });
  return {
    app,
    headlessCodexStack: stack,
    headlessCodex: Template.fromStack(stack),
    healthcare: Template.fromStack(healthcareStack),
  };
}

function taskRoleStatements(template: Template): IamStatement[] {
  const taskDefinitions = Object.values(
    template.findResources('AWS::ECS::TaskDefinition'),
  ) as CfnResource[];
  expect(taskDefinitions).toHaveLength(1);

  const taskRoleArn = taskDefinitions[0].Properties?.TaskRoleArn as
    TaskRoleArn | undefined;
  const taskRoleLogicalId = taskRoleArn?.['Fn::GetAtt']?.[0];
  expect(taskRoleLogicalId).toEqual(expect.any(String));

  const policies = Object.values(
    template.findResources('AWS::IAM::Policy'),
  ) as CfnResource[];
  return policies
    .filter((resource) => {
      const roles = (resource.Properties?.Roles ?? []) as {
        Ref?: string;
      }[];
      return roles.some((role) => role.Ref === taskRoleLogicalId);
    })
    .flatMap(
      (resource) =>
        (resource.Properties?.PolicyDocument as PolicyDocument | undefined)
          ?.Statement ?? [],
    );
}

test('the analysis task is not told how to reach the service it investigates', () => {
  // Recovery moved behind a user approval gate, so analysis has no reason to
  // know the target's address. Leaving the identity here would keep a route
  // available to a path that must not write.
  const { headlessCodex } = synthesize();

  expect(JSON.stringify(headlessCodex.toJSON())).not.toContain('HEALTHCARE_');
});

test('Codex queue visibility exceeds the analysis and staleness boundary', () => {
  const { headlessCodex } = synthesize();
  const queues = Object.values(
    headlessCodex.findResources('AWS::SQS::Queue'),
  ) as CfnResource[];
  const alarmQueue = queues.find(
    (resource) =>
      resource.Properties?.QueueName === 'RcaAgentDevCcHeadlessQueue',
  );
  const visibilityTimeout = alarmQueue?.Properties?.VisibilityTimeout;
  // 분석 예산과 오래된 알람 기준이 같은 값이므로 경계도 하나다. 가시성이 이 경계보다
  // 짧으면 아직 돌고 있는 세션의 메시지가 재전달되어 같은 알람을 두 번 분석한다.
  const analysisAndStalenessBoundarySeconds = 60 * 60;

  expect(visibilityTimeout).toBe(65 * 60);
  expect(visibilityTimeout as number).toBeGreaterThan(
    analysisAndStalenessBoundarySeconds,
  );
});

test('Codex uses the global GPT-5.6 Sol profile at high reasoning', () => {
  const { headlessCodex } = synthesize();

  headlessCodex.hasResourceProperties('AWS::ECS::TaskDefinition', {
    ContainerDefinitions: Match.arrayWith([
      Match.objectLike({
        Environment: Match.arrayWith([
          { Name: 'CODEX_MODEL', Value: 'global.openai.gpt-5.6-sol' },
          { Name: 'CODEX_REASONING_EFFORT', Value: 'high' },
          { Name: 'CODEX_MODEL_PROVIDER', Value: 'amazon-bedrock-runtime' },
          {
            Name: 'CODEX_BEDROCK_BASE_URL',
            Value: {
              'Fn::Join': [
                '',
                [
                  'https://bedrock-runtime.',
                  { Ref: 'AWS::Region' },
                  '.amazonaws.com/openai/v1',
                ],
              ],
            },
          },
        ]),
      }),
    ]),
  });
});

test('Codex task role can exchange AWS credentials for a Bedrock bearer token', () => {
  const { headlessCodex } = synthesize();
  const actions = taskRoleStatements(headlessCodex)
    .filter((statement) => statement.Effect !== 'Deny')
    .flatMap((statement) =>
      Array.isArray(statement.Action) ? statement.Action : [statement.Action],
    );

  expect(actions).toContain('bedrock:CallWithBearerToken');
});

test('Healthcare opens no ingress for the analysis engine', () => {
  const { healthcare } = synthesize();
  const ingressRules = Object.values(
    healthcare.findResources('AWS::EC2::SecurityGroupIngress'),
  ) as CfnResource[];

  expect(ingressRules).toEqual([]);
});

test('Codex task role has no ECS service write permissions', () => {
  const { headlessCodex } = synthesize();
  const statements = taskRoleStatements(headlessCodex);
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

test('Codex can read but cannot alter an approved snapshot', () => {
  const { headlessCodex } = synthesize();
  const statements = taskRoleStatements(headlessCodex);
  const approvalDeny = statements.find((statement) => {
    const actions = Array.isArray(statement.Action)
      ? statement.Action
      : [statement.Action];
    return (
      statement.Effect === 'Deny' &&
      actions.includes('s3:PutObject') &&
      actions.includes('s3:DeleteObject')
    );
  });
  const allowedActions = statements
    .filter((statement) => statement.Effect !== 'Deny')
    .flatMap((statement) =>
      Array.isArray(statement.Action) ? statement.Action : [statement.Action],
    );

  expect(JSON.stringify(approvalDeny?.Resource)).toContain('approvals/*');
  expect(allowedActions).toContain('s3:GetObject*');
});

test('the analysis task role holds no write permission at all', () => {
  const { headlessCodex } = synthesize();
  const actions = taskRoleStatements(headlessCodex)
    .flatMap((statement) =>
      Array.isArray(statement.Action) ? statement.Action : [statement.Action],
    )
    .filter((action): action is string => typeof action === 'string');

  // Writes analysis does need: its own artifacts, the session record, its
  // notification, and the plumbing to consume its queue and be shelled into.
  // Any other mutating action would put recovery back inside the analysis path.
  const allowedWritePrefixes = [
    's3:Put',
    's3:Delete',
    's3:Abort',
    's3vectors:',
    'dynamodb:',
    'sns:Publish',
    'logs:',
    'sqs:ReceiveMessage',
    'sqs:DeleteMessage',
    'sqs:ChangeMessageVisibility',
    'ssmmessages:',
  ];
  const unexpectedWrites = actions.filter((action) => {
    const isRead = /:(Get|List|Describe|Head|Query|Scan|BatchGet|Lookup)/.test(
      action,
    );
    const isAllowed = allowedWritePrefixes.some((prefix) =>
      action.startsWith(prefix),
    );
    return !isRead && !isAllowed && !action.startsWith('bedrock:');
  });

  expect(unexpectedWrites).toEqual([]);
});
