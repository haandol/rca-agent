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

function synthesize(): Template {
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
    tracing: false,
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
