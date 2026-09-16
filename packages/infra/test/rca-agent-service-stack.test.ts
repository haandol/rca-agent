import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import { Template } from 'aws-cdk-lib/assertions';

import { RcaAgentServiceStack } from '../lib/stacks/rca-agent-service-stack';

type CfnResource = {
  Properties?: Record<string, unknown>;
};

type IamStatement = {
  Effect?: string;
  Action?: string | string[];
  Resource?: unknown;
};

type TaskRoleArn = {
  'Fn::GetAtt'?: [string, string];
};

/** Synthesize the actual task with optional tracing to check its shared network controls. */
function synthesize(tracing = false): Template {
  const app = new cdk.App({ context: { ns: 'RcaAgentDev' } });
  const dependencies = new cdk.Stack(app, 'Dependencies');
  const vpc = new ec2.Vpc(dependencies, 'Vpc', { maxAzs: 2 });
  const alarmQueue = new sqs.Queue(dependencies, 'AlarmQueue');
  const notificationTopic = new sns.Topic(dependencies, 'NotificationTopic');
  const sessionTable = new dynamodb.Table(dependencies, 'SessionTable', {
    partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
  });
  const evidenceBucket = new s3.Bucket(dependencies, 'EvidenceBucket');

  const stack = new RcaAgentServiceStack(app, 'RcaAgentTest', {
    vpc,
    alarmQueue,
    notificationTopic,
    rcaSessionTable: sessionTable,
    evidenceBucket,
    vectorBucketName: 'rca-test-vectors',
    imageTag: 'latest',
    tracing,
  });
  return Template.fromStack(stack);
}

function taskRoleStatements(template: Template): IamStatement[] {
  const taskDefinition = Object.values(
    template.findResources('AWS::ECS::TaskDefinition'),
  )[0] as CfnResource;
  const taskRoleArn = taskDefinition.Properties?.TaskRoleArn as TaskRoleArn;
  const taskRoleLogicalId = taskRoleArn['Fn::GetAtt']?.[0];

  return (
    Object.values(template.findResources('AWS::IAM::Policy')) as CfnResource[]
  )
    .filter((policy) => {
      const roles = (policy.Properties?.Roles ?? []) as { Ref?: string }[];
      return roles.some((role) => role.Ref === taskRoleLogicalId);
    })
    .flatMap((policy) => {
      const document = policy.Properties?.PolicyDocument as {
        Statement?: IamStatement[];
      };
      return document.Statement ?? [];
    });
}

test('Strands can read but cannot alter an approved snapshot', () => {
  const statements = taskRoleStatements(synthesize());
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

test('Strands can inspect deployment identity without ECS mutation permission', () => {
  const actions = taskRoleStatements(synthesize())
    .filter((statement) => statement.Effect !== 'Deny')
    .flatMap((statement) =>
      Array.isArray(statement.Action) ? statement.Action : [statement.Action],
    )
    .filter((action): action is string => Boolean(action?.startsWith('ecs:')));

  expect(actions.sort()).toEqual(
    [
      'ecs:DescribeServices',
      'ecs:DescribeTaskDefinition',
      'ecs:DescribeTasks',
      'ecs:ListTasks',
    ].sort(),
  );
});

test('deployed Strands pins the approved admission budgets and output limit', () => {
  const definitions = Object.values(
    synthesize().findResources('AWS::ECS::TaskDefinition'),
  ) as CfnResource[];
  const containers = definitions[0]?.Properties?.ContainerDefinitions as {
    Name: string;
    Environment: { Name: string; Value: string }[];
  }[];
  const app = containers.find((container) => container.Name === 'rca-agent');
  const environment = Object.fromEntries(
    (app?.Environment ?? []).map((entry) => [entry.Name, entry.Value]),
  );

  expect(environment).toEqual(
    expect.objectContaining({
      RCA_TIME_BUDGET_SECONDS: '3600',
      SCOPING_TIMEOUT_SECONDS: '900',
      HYPOTHESIS_GENERATION_TIMEOUT_SECONDS: '900',
      LLM_DEFAULT_TIMEOUT_SECONDS: '900',
      EVIDENCE_COLLECTION_TIMEOUT_SECONDS: '1800',
      BEDROCK_MAX_TOKENS: '65536',
      ALARM_STALENESS_SECONDS: '10800',
    }),
  );
});

test.each([false, true])(
  'only the RCA app sets the shared awsvpc keepalive idle time with tracing=%s',
  (tracing) => {
    const template = synthesize(tracing);
    const definitions = Object.values(
      template.findResources('AWS::ECS::TaskDefinition'),
    ) as CfnResource[];
    expect(definitions).toHaveLength(1);
    const properties = definitions[0].Properties!;
    expect(properties.NetworkMode).toBe('awsvpc');
    expect(properties.RequiresCompatibilities).toEqual(['FARGATE']);
    expect(properties.RuntimePlatform).toEqual({
      CpuArchitecture: 'ARM64',
      OperatingSystemFamily: 'LINUX',
    });
    const containers = properties.ContainerDefinitions as {
      Name: string;
      SystemControls?: { Namespace: string; Value: string }[];
    }[];
    expect(
      containers.some((container) => container.Name === 'otel-collector'),
    ).toBe(tracing);
    const controls = containers.flatMap((container) =>
      (container.SystemControls ?? []).map((control) => ({
        container: container.Name,
        ...control,
      })),
    );
    expect(controls).toEqual([
      {
        container: 'rca-agent',
        Namespace: 'net.ipv4.tcp_keepalive_time',
        Value: '120',
      },
    ]);
    // Exact equality also excludes sidecar overrides and interval/probe tuning.
  },
);
