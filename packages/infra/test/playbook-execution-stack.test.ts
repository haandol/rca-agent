import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as fs from 'fs';
import * as path from 'path';
import * as toml from 'toml';
import { Match, Template } from 'aws-cdk-lib/assertions';

import { PlaybookExecutionStack } from '../lib/stacks/playbook-execution-stack';

type CfnResource = {
  Properties?: Record<string, unknown>;
};

type IamStatement = {
  Effect?: string;
  Action?: string | string[];
  Resource?: unknown;
  Condition?: Record<string, Record<string, unknown>>;
};

type PolicyDocument = {
  Statement?: IamStatement[];
};

type TaskRoleArn = {
  'Fn::GetAtt'?: [string, string];
};

type Synthesized = {
  execution: Template;
  healthcare: Template;
};

const QUEUE_NAME = 'RcaAgentDevPlaybookExecutionQueue';
const DLQ_NAME = 'RcaAgentDevPlaybookExecutionDLQ';

function synthesize(): Synthesized {
  const app = new cdk.App({ context: { ns: 'RcaAgentDev' } });

  const dependencies = new cdk.Stack(app, 'Dependencies');
  const vpc = new ec2.Vpc(dependencies, 'Vpc', { maxAzs: 2 });
  const sessionTable = new dynamodb.Table(dependencies, 'SessionTable', {
    partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
    sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
  });
  const evidenceBucket = new s3.Bucket(dependencies, 'EvidenceBucket');

  // Healthcare sits in its own stack so the ingress cross-reference flows one
  // way — Healthcare → execution — exactly as in production.
  const healthcareStack = new cdk.Stack(app, 'Healthcare');
  const cluster = new ecs.Cluster(healthcareStack, 'HcCluster', { vpc });
  const taskDefinition = new ecs.FargateTaskDefinition(
    healthcareStack,
    'HcTask',
  );
  taskDefinition.addContainer('hc', {
    image: ecs.ContainerImage.fromRegistry('placeholder'),
    portMappings: [{ containerPort: 8000 }],
  });
  const healthcareService = new ecs.FargateService(
    healthcareStack,
    'HcService',
    { cluster, taskDefinition },
  );

  const stack = new PlaybookExecutionStack(app, 'PlaybookExecutionTest', {
    vpc,
    healthcareService,
    rcaSessionTable: sessionTable,
    evidenceBucket,
    vectorBucketName: 'rca-test-vectors',
    imageTag: 'latest',
  });

  return {
    execution: Template.fromStack(stack),
    healthcare: Template.fromStack(healthcareStack),
  };
}

function taskRoleStatements(template: Template): IamStatement[] {
  const taskDefinitions = Object.values(
    template.findResources('AWS::ECS::TaskDefinition'),
  ) as CfnResource[];
  expect(taskDefinitions).toHaveLength(1);
  const taskRoleArn = taskDefinitions[0].Properties?.TaskRoleArn as TaskRoleArn;
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

function taskRole(template: Template): CfnResource {
  const taskDefinition = Object.values(
    template.findResources('AWS::ECS::TaskDefinition'),
  )[0] as CfnResource;
  const taskRoleArn = taskDefinition.Properties?.TaskRoleArn as TaskRoleArn;
  const taskRoleLogicalId = taskRoleArn?.['Fn::GetAtt']?.[0];
  if (!taskRoleLogicalId) {
    throw new Error('Synthesized task definition has no task role');
  }
  const role = template.findResources('AWS::IAM::Role')[taskRoleLogicalId];
  expect(role).toBeDefined();
  return role as CfnResource;
}

function queue(template: Template, name: string): CfnResource | undefined {
  return (
    Object.values(template.findResources('AWS::SQS::Queue')) as CfnResource[]
  ).find((resource) => resource.Properties?.QueueName === name);
}

test('no subscription can start an execution without a user request', () => {
  // Approval is the message. An event subscription here would mean a task starts
  // whether or not a person asked for it, leaving the gate as a code condition
  // rather than a property of the path.
  const { execution } = synthesize();

  expect(
    Object.values(execution.findResources('AWS::SNS::Subscription')),
  ).toEqual([]);
  expect(Object.values(execution.findResources('AWS::SNS::Topic'))).toEqual([]);
});

test('the request queue outlives the worst-case execution', () => {
  // A visibility timeout shorter than an execution redelivers the request while
  // it is still running, and the same approval would execute twice.
  const { execution } = synthesize();
  const request = queue(execution, QUEUE_NAME);
  const executionTimeoutSeconds = 3600;

  expect(request?.Properties?.VisibilityTimeout as number).toBeGreaterThan(
    executionTimeoutSeconds,
  );
  expect(request?.Properties).toEqual(
    expect.objectContaining({
      MessageRetentionPeriod: 4 * 24 * 60 * 60,
      RedrivePolicy: expect.objectContaining({
        deadLetterTargetArn: expect.any(Object),
        maxReceiveCount: 3,
      }),
    }),
  );
});

test('a repeatedly failing request moves to a retained dead-letter queue', () => {
  const { execution } = synthesize();

  expect(queue(execution, DLQ_NAME)?.Properties).toEqual(
    expect.objectContaining({
      MessageRetentionPeriod: 14 * 24 * 60 * 60,
      VisibilityTimeout: 10 * 60,
    }),
  );
});

test('the worker runs the execution entrypoint of the analysis image', () => {
  const { execution } = synthesize();

  execution.hasResourceProperties('AWS::ECS::TaskDefinition', {
    ContainerDefinitions: Match.arrayWith([
      Match.objectLike({
        Name: 'playbook-execution',
        Command: ['python', '-m', 'headless_codex.execution_main'],
        Environment: Match.arrayWith([
          {
            Name: 'CODEX_MODEL',
            Value: 'global.openai.gpt-5.6-sol',
          },
          { Name: 'CODEX_REASONING_EFFORT', Value: 'high' },
          {
            Name: 'CODEX_MODEL_PROVIDER',
            Value: 'amazon-bedrock-runtime',
          },
          Match.objectLike({ Name: 'EXECUTION_QUEUE_URL' }),
          { Name: 'EXECUTION_TIMEOUT_SECONDS', Value: '3600' },
        ]),
      }),
    ]),
  });
});

test('the worker stays running because the queue decides whether anything runs', () => {
  const { execution } = synthesize();

  execution.hasResourceProperties('AWS::ECS::Service', { DesiredCount: 1 });
});

test('execution reaches the target service, unlike analysis', () => {
  const { healthcare } = synthesize();
  const ingressRules = Object.values(
    healthcare.findResources('AWS::EC2::SecurityGroupIngress'),
  ) as CfnResource[];

  expect(ingressRules).toHaveLength(1);
  expect(ingressRules[0].Properties).toEqual(
    expect.objectContaining({
      Description: 'Playbook execution acting on the Healthcare service',
      FromPort: 8000,
      ToPort: 8000,
      IpProtocol: 'tcp',
      SourceSecurityGroupId: expect.objectContaining({
        'Fn::ImportValue': expect.any(String),
      }),
    }),
  );
});

test('the execution role can write, which is what separates it from analysis', () => {
  const { execution } = synthesize();
  const managedPolicies = JSON.stringify(
    taskRole(execution).Properties?.ManagedPolicyArns,
  );

  expect(managedPolicies).toContain('PowerUserAccess');
});

test('PowerUserAccess cannot publish another request to the execution queue', () => {
  const { execution } = synthesize();
  const sendDeny = taskRoleStatements(execution).find((statement) => {
    const actions = Array.isArray(statement.Action)
      ? statement.Action
      : [statement.Action];
    return statement.Effect === 'Deny' && actions.includes('sqs:SendMessage');
  });

  expect(sendDeny).toBeDefined();
  expect(JSON.stringify(sendDeny?.Resource)).toContain('RequestQueue');
  expect(JSON.stringify(execution.toJSON())).toContain('PowerUserAccess');
});

test('execution can read but cannot alter an approved snapshot', () => {
  const { execution } = synthesize();
  const statements = taskRoleStatements(execution);
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

test('account, billing, and identity scopes are denied outright', () => {
  // These are never an execution target, so they are excluded by the role rather
  // than left to the tool's judgment.
  const { execution } = synthesize();
  const denied = taskRoleStatements(execution)
    .filter((statement) => statement.Effect === 'Deny')
    .flatMap((statement) =>
      Array.isArray(statement.Action) ? statement.Action : [statement.Action],
    );

  for (const scope of [
    'organizations:*',
    'account:*',
    'billing:*',
    'budgets:*',
    'ce:*',
    'cur:*',
    'sso:*',
    'identitystore:*',
  ]) {
    expect(denied).toContain(scope);
  }
});

test('the execution role cannot grant or revoke access', () => {
  const { execution } = synthesize();
  const denied = taskRoleStatements(execution)
    .filter((statement) => statement.Effect === 'Deny')
    .flatMap((statement) =>
      Array.isArray(statement.Action) ? statement.Action : [statement.Action],
    );

  for (const mutation of [
    'iam:Create*',
    'iam:Delete*',
    'iam:Update*',
    'iam:Put*',
    'iam:Attach*',
    'iam:Detach*',
  ]) {
    expect(denied).toContain(mutation);
  }
});

test('a rollback can hand ECS the task definition it rolls back to', () => {
  // Rolling back to a known-good task definition is an allowed recovery action,
  // and ECS refuses one unless the caller may pass that definition's roles. A
  // live run hit AccessDenied on exactly this while force-new-deployment passed.
  const { execution } = synthesize();
  const passRole = taskRoleStatements(execution).filter((statement) => {
    const actions = Array.isArray(statement.Action)
      ? statement.Action
      : [statement.Action];
    return statement.Effect !== 'Deny' && actions.includes('iam:PassRole');
  });

  expect(passRole).toHaveLength(1);
  // Passing a role to any other service would be a privilege-escalation path,
  // and no recovery step needs one.
  expect(passRole[0].Condition?.StringEquals).toEqual({
    'iam:PassedToService': 'ecs-tasks.amazonaws.com',
  });
  expect(passRole[0].Resource).toHaveLength(2);
  const resources = JSON.stringify(passRole[0].Resource);
  expect(resources).toContain('RcaAgentDevHealthcareTaskRole');
  expect(resources).toContain('RcaAgentDevHealthcareExecutionRole');
});

test('destructive-action refusal is not expressed as an IAM deny', () => {
  // Operation-name vocabulary and IAM action names do not map one-to-one, so a
  // policy-level refusal list would leave gaps and could not record why a step
  // was blocked. The execution tool owns that judgment.
  const { execution } = synthesize();
  const denied = taskRoleStatements(execution)
    .filter((statement) => statement.Effect === 'Deny')
    .flatMap((statement) =>
      Array.isArray(statement.Action) ? statement.Action : [statement.Action],
    )
    .filter((action): action is string => typeof action === 'string');

  expect(denied.filter((action) => action.startsWith('ecs:'))).toEqual([]);
  expect(denied.filter((action) => action.startsWith('rds:'))).toEqual([]);
  expect(denied.filter((action) => action.startsWith('ec2:'))).toEqual([]);
});

test('the execution role can persist evidence and reindex a revised playbook', () => {
  const { execution } = synthesize();
  const actions = taskRoleStatements(execution)
    .filter((statement) => statement.Effect !== 'Deny')
    .flatMap((statement) =>
      Array.isArray(statement.Action) ? statement.Action : [statement.Action],
    );

  expect(actions).toEqual(
    expect.arrayContaining([
      's3vectors:PutVectors',
      'bedrock:CallWithBearerToken',
      'bedrock:InvokeModel',
      'sqs:ReceiveMessage',
    ]),
  );
  expect(actions.filter((action) => action === 's3:PutObject')).not.toEqual([]);
  expect(
    actions.filter((action) => action === 'dynamodb:UpdateItem'),
  ).not.toEqual([]);
});

test('the execution worker cannot publish the analysis notification', () => {
  // The notification says analysis finished. Execution has its own state and
  // must not be able to speak for the analysis pipeline.
  const { execution } = synthesize();
  const actions = taskRoleStatements(execution).flatMap((statement) =>
    Array.isArray(statement.Action) ? statement.Action : [statement.Action],
  );

  expect(actions).not.toContain('sns:Publish');
});

test('dev pins no image tag and no feature-flag task count', () => {
  const config = toml.parse(
    fs.readFileSync(path.resolve(__dirname, '../config/dev.toml'), 'utf-8'),
  ) as {
    execution?: { imageTag?: string; desiredCount?: number };
    agent?: { imageTag?: string };
    headlessCodex?: { imageTag?: string };
    healthcare?: { imageTag?: string };
  };

  // A default tag here would let a deploy that injects no tag silently fall back
  // to it. CDK updates the stacks a target depends on, so that fallback would
  // rewrite an unrelated service's task definition to a stale image — which is
  // exactly how the execution worker first came up without its entry point.
  for (const service of [
    'agent',
    'headlessCodex',
    'healthcare',
    'execution',
  ] as const) {
    expect(config[service]?.imageTag).toBeUndefined();
  }

  // Gating by task count made sense when an event subscription was the trigger.
  expect(config.execution?.desiredCount).toBeUndefined();
});
