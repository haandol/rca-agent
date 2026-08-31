import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';

import { EventBusStack } from '../lib/stacks/event-bus-stack';

const NS = 'RcaAgentTest';

function synthesizeEventBus(): Template {
  const app = new cdk.App({ context: { ns: NS } });
  const stack = new EventBusStack(app, `${NS}EventBusStack`, {
    env: { account: '111122223333', region: 'us-east-1' },
    notificationEmail: 'ops@example.com',
  });
  return Template.fromStack(stack);
}

/**
 * A synthesized topic-policy statement.
 *
 * Values are unions of literals and CloudFormation intrinsics, so the shape is
 * read loosely and each assertion narrows what it needs.
 */
type PolicyStatement = {
  Effect?: string;
  Action?: unknown;
  Principal?: { Service?: string };
  Condition?: Record<string, Record<string, unknown>>;
};

function topicPolicyStatements(template: Template): PolicyStatement[] {
  const policies = template.findResources('AWS::SNS::TopicPolicy');
  return Object.values(policies).flatMap(
    (policy) =>
      (policy.Properties?.PolicyDocument?.Statement ?? []) as PolicyStatement[],
  );
}

/** The statement that lets CloudWatch publish alarms into the RCA topic. */
function cloudwatchPublishStatement(template: Template): PolicyStatement {
  const match = topicPolicyStatements(template).find(
    (statement) => statement?.Principal?.Service === 'cloudwatch.amazonaws.com',
  );
  expect(match).toBeDefined();
  return match!;
}

// An alarm arriving on this topic starts a full RCA run on both engines, and the
// path is unauthenticated. So who may publish is the only thing standing between
// a foreign alarm and unbounded model spend.
test('only this account may publish alarms to the RCA topic', () => {
  const statement = cloudwatchPublishStatement(synthesizeEventBus());

  expect(statement.Action).toBe('sns:Publish');
  expect(statement.Effect).toBe('Allow');
  // Without SourceAccount, every account using CloudWatch is authorized —
  // service principals are not account-scoped on their own.
  expect(statement.Condition?.StringEquals?.['aws:SourceAccount']).toBe(
    '111122223333',
  );
});

test('only an alarm this deployment owns may publish', () => {
  const statement = cloudwatchPublishStatement(synthesizeEventBus());

  const sourceArn = statement.Condition?.ArnLike?.['aws:SourceArn'];
  expect(sourceArn).toBeDefined();

  // The alarms live in sibling stacks in this account, so the grant is scoped by
  // this deployment's alarm-name prefix. A same-account alarm outside the
  // namespace — someone else's unrelated alarm — must not reach the engines.
  const rendered = JSON.stringify(sourceArn);
  expect(rendered).toContain(':cloudwatch:');
  expect(rendered).toContain(':alarm:');
  expect(rendered).toContain(`${NS}-*`);
});

test('the topic requires TLS and delivers alarms to the queue unwrapped', () => {
  const template = synthesizeEventBus();

  // Raw delivery keeps the alarm payload intact: the engines parse the alarm's
  // own fields, not an SNS envelope around them.
  template.hasResourceProperties(
    'AWS::SNS::Subscription',
    Match.objectLike({ Protocol: 'sqs', RawMessageDelivery: true }),
  );

  const denyInsecure = topicPolicyStatements(template).find(
    (statement) =>
      statement?.Effect === 'Deny' &&
      statement?.Condition?.Bool?.['aws:SecureTransport'] === 'false',
  );
  expect(denyInsecure).toBeDefined();
});

test('analysis completion is isolated from incident ingestion', () => {
  const template = synthesizeEventBus();
  const topics = template.findResources('AWS::SNS::Topic');
  const alarmTopic = Object.entries(topics).find(
    ([, topic]) => topic.Properties?.TopicName === `${NS}Alarm`,
  );
  const notificationTopic = Object.entries(topics).find(
    ([, topic]) => topic.Properties?.TopicName === `${NS}AnalysisCompletion`,
  );
  expect(alarmTopic).toBeDefined();
  expect(notificationTopic).toBeDefined();

  const subscriptions = Object.values(
    template.findResources('AWS::SNS::Subscription'),
  );
  const queueSubscription = subscriptions.find(
    (subscription) => subscription.Properties?.Protocol === 'sqs',
  );
  const humanSubscription = subscriptions.find(
    (subscription) => subscription.Properties?.Protocol === 'email',
  );

  expect(queueSubscription?.Properties?.TopicArn).toEqual({
    Ref: alarmTopic![0],
  });
  expect(humanSubscription?.Properties).toEqual(
    expect.objectContaining({
      Endpoint: 'ops@example.com',
      TopicArn: { Ref: notificationTopic![0] },
    }),
  );
});

test('failed alarm messages are retained rather than dropped', () => {
  const template = synthesizeEventBus();

  // A dropped alarm is an incident nobody analyzed, so exhausted retries land in
  // the DLQ for manual reprocessing.
  template.hasResourceProperties(
    'AWS::SQS::Queue',
    Match.objectLike({
      QueueName: `${NS}AlarmQueue`,
      RedrivePolicy: Match.objectLike({ maxReceiveCount: 3 }),
    }),
  );
});

test('the shared queue covers the longest analysis budget', () => {
  const template = synthesizeEventBus();

  template.hasResourceProperties(
    'AWS::SQS::Queue',
    Match.objectLike({
      QueueName: `${NS}AlarmQueue`,
      VisibilityTimeout: 65 * 60,
    }),
  );
});
