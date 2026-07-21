import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as sns from 'aws-cdk-lib/aws-sns';
import { Match, Template } from 'aws-cdk-lib/assertions';

import { RemediationAgentStack } from '../lib/stacks/remediation-agent-stack';

function synthesize(desiredCount: number): Template {
  const app = new cdk.App({ context: { ns: 'RcaAgentDev' } });

  // 프로덕션과 동일하게 Healthcare 서비스를 별도 스택에 두어, SG 인그레스 교차
  // 참조가 Healthcare → Remediation 한 방향으로만 흐르도록 한다.
  const netStack = new cdk.Stack(app, 'NetStack');
  const vpc = new ec2.Vpc(netStack, 'Vpc', { maxAzs: 2 });
  const topic = new sns.Topic(netStack, 'NotificationTopic');

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
    healthcareClusterName: 'RcaAgentDevHealthcare',
    healthcareServiceName: 'RcaAgentDevHealthcare',
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

  const queues = template.findResources('AWS::SQS::Queue');
  const names = Object.values(queues).map((q) => q.Properties.QueueName);
  expect(names).toEqual(
    expect.arrayContaining(['RcaAgentDevRemediationQueue', 'RcaAgentDevRemediationDLQ']),
  );

  template.hasResourceProperties('AWS::SQS::Queue', {
    QueueName: 'RcaAgentDevRemediationQueue',
    RedrivePolicy: Match.objectLike({ maxReceiveCount: 3 }),
  });
});

test('task runs the remediation entrypoint and can force ECS deployment', () => {
  const template = synthesize(1);

  template.hasResourceProperties('AWS::ECS::TaskDefinition', {
    ContainerDefinitions: Match.arrayWith([
      Match.objectLike({
        Command: ['python', '-m', 'rca_agent.remediation_main'],
      }),
    ]),
  });

  template.hasResourceProperties('AWS::IAM::Policy', {
    PolicyDocument: {
      Statement: Match.arrayWith([
        Match.objectLike({
          Action: Match.arrayWith(['ecs:UpdateService', 'ecs:DescribeServices']),
        }),
      ]),
    },
  });
});

test('desiredCount 0 leaves the feature flag off (analysis-only)', () => {
  const template = synthesize(0);

  template.hasResourceProperties('AWS::ECS::Service', {
    DesiredCount: 0,
  });
});
