import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as sns from 'aws-cdk-lib/aws-sns';
import { Match, Template } from 'aws-cdk-lib/assertions';

import { HealthcareServiceStack } from '../lib/stacks/healthcare-service-stack';
import { RdsStack } from '../lib/stacks/rds-stack';

type CfnResource = {
  Properties?: Record<string, unknown>;
};

type MetricQuery = {
  MetricStat?: {
    Metric?: {
      MetricName?: string;
    };
  };
};

function synthesize(): Template {
  const app = new cdk.App({ context: { ns: 'RcaAgentDev' } });
  const network = new cdk.Stack(app, 'Network');
  const vpc = new ec2.Vpc(network, 'Vpc', { maxAzs: 2 });
  const alarmTopic = new sns.Topic(network, 'AlarmTopic');
  const database = new RdsStack(app, 'Database', { vpc });

  const stack = new HealthcareServiceStack(app, 'HealthcareTest', {
    vpc,
    dbInstance: database.instance,
    alarmTopic,
    imageTag: 'latest',
    tracing: false,
  });
  return Template.fromStack(stack);
}

function alarmMetricNames(template: Template): string[] {
  const alarms = Object.values(
    template.findResources('AWS::CloudWatch::Alarm'),
  ) as CfnResource[];

  return alarms.flatMap((alarm) => {
    const directMetric = alarm.Properties?.MetricName;
    const metricQueries = (alarm.Properties?.Metrics ?? []) as MetricQuery[];
    const queryMetrics = metricQueries
      .map((query) => query.MetricStat?.Metric?.MetricName)
      .filter((metricName: unknown): metricName is string =>
        Boolean(metricName),
      );
    return typeof directMetric === 'string'
      ? [directMetric, ...queryMetrics]
      : queryMetrics;
  });
}

test('the RCA entry alarm watches a domain symptom metric', () => {
  const template = synthesize();

  template.hasResourceProperties('AWS::CloudWatch::Alarm', {
    AlarmName: 'RcaAgentDev-Healthcare-VitalIngestFailures',
    Namespace: 'Healthcare/Sensor',
    MetricName: 'VitalIngestFailures',
    Dimensions: [{ Name: 'ServiceName', Value: 'healthcare-sensor-app' }],
  });
});

test('the entry alarm does not name the subsystem that caused the failure', () => {
  const alarms = Object.values(
    synthesize().findResources('AWS::CloudWatch::Alarm'),
  ) as CfnResource[];
  const entryAlarm = alarms.find(
    (alarm) =>
      alarm.Properties?.AlarmName ===
      'RcaAgentDev-Healthcare-VitalIngestFailures',
  );
  const text = [
    entryAlarm?.Properties?.AlarmName,
    entryAlarm?.Properties?.AlarmDescription,
  ]
    .join(' ')
    .toLowerCase();

  for (const causeTerm of [
    'connection',
    'pool',
    'leak',
    'cpu',
    'memory',
    'query',
    'deploy',
  ]) {
    expect(text).not.toContain(causeTerm);
  }
});

test('cause-level alarms remain available as evidence', () => {
  const metricNames = alarmMetricNames(synthesize());

  expect(metricNames).toContain('DatabaseConnections');
  expect(metricNames).toContain('CPUUtilization');
  expect(metricNames).toContain('MemoryUtilization');
});

test('only the symptom alarm publishes state changes to the RCA topic', () => {
  const alarms = Object.values(
    synthesize().findResources('AWS::CloudWatch::Alarm'),
  ) as CfnResource[];
  const entryAlarmName = 'RcaAgentDev-Healthcare-VitalIngestFailures';
  const alarmsWithActions = alarms.filter(
    (alarm) => (alarm.Properties?.AlarmActions as unknown[] | undefined)?.length,
  );
  const alarmsWithOkActions = alarms.filter(
    (alarm) => (alarm.Properties?.OKActions as unknown[] | undefined)?.length,
  );

  expect(
    alarmsWithActions.map((alarm) => alarm.Properties?.AlarmName),
  ).toEqual([entryAlarmName]);
  expect(
    alarmsWithOkActions.map((alarm) => alarm.Properties?.AlarmName),
  ).toEqual([entryAlarmName]);

  const alarmActions = alarmsWithActions[0].Properties
    ?.AlarmActions as unknown[];
  const okActions = alarmsWithOkActions[0].Properties?.OKActions as unknown[];
  expect(alarmActions).toHaveLength(1);
  expect(okActions).toEqual(alarmActions);
  expect(alarmActions[0]).toEqual({
    'Fn::ImportValue': expect.stringContaining('AlarmTopic'),
  });
});

test('the deployed revision is exposed to the container', () => {
  synthesize().hasResourceProperties('AWS::ECS::TaskDefinition', {
    ContainerDefinitions: Match.arrayWith([
      Match.objectLike({
        Name: 'healthcare',
        Environment: Match.arrayWith([
          { Name: 'DEPLOYED_REVISION', Value: 'latest' },
        ]),
      }),
    ]),
  });
});

test('task roles have stable names for scoped pass-role permission', () => {
  const roles = Object.values(
    synthesize().findResources('AWS::IAM::Role'),
  ) as CfnResource[];
  const roleNames = roles
    .map((role) => role.Properties?.RoleName)
    .filter((roleName): roleName is string => typeof roleName === 'string');

  expect(roleNames).toEqual(
    expect.arrayContaining([
      'RcaAgentDevHealthcareTaskRole',
      'RcaAgentDevHealthcareExecutionRole',
    ]),
  );
});

function alarmThreshold(template: Template, alarmName: string): number {
  const alarms = Object.values(
    template.findResources('AWS::CloudWatch::Alarm'),
  ) as CfnResource[];
  const alarm = alarms.find(
    (candidate) => candidate.Properties?.AlarmName === alarmName,
  );
  expect(alarm).toBeDefined();
  return alarm!.Properties!.Threshold as number;
}

// The demo only holds together if a leak actually moves the metrics: the
// cause-level alarm has to trip before the pool runs dry, and the pool has to run
// dry for the symptom alarm to see anything at all. That ordering is the
// contract, so it is pinned here rather than left to whoever next edits a number.
const POOL_SIZE = 5;
const POOL_MAX_OVERFLOW = 10;
const POOL_CEILING = POOL_SIZE + POOL_MAX_OVERFLOW;
const MEASURED_NORMAL_CONNECTIONS = 3;

test('the connection alarm trips between normal usage and pool exhaustion', () => {
  const threshold = alarmThreshold(
    synthesize(),
    'RcaAgentDev-Healthcare-RdsHighConnections',
  );

  // Above the ceiling the leak would starve requests while this metric stayed
  // quiet, so the evidence the agent must find would not exist.
  expect(threshold).toBeLessThan(POOL_CEILING);
  // Too close to normal usage and ordinary traffic variation trips it.
  expect(threshold).toBeGreaterThan(MEASURED_NORMAL_CONNECTIONS * 2);
});

test('the container pool is the capacity the threshold was derived from', () => {
  // The threshold is only meaningful relative to the pool. If the pool moves and
  // this is not revisited, the ordering above silently stops holding.
  synthesize().hasResourceProperties('AWS::ECS::TaskDefinition', {
    ContainerDefinitions: Match.arrayWith([
      Match.objectLike({
        Name: 'healthcare',
        Environment: Match.arrayWith([
          { Name: 'DB_POOL_SIZE', Value: String(POOL_SIZE) },
          { Name: 'DB_MAX_OVERFLOW', Value: String(POOL_MAX_OVERFLOW) },
        ]),
      }),
    ]),
  });
});
