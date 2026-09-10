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

/** Synthesize a private demo service with optional workload tuning and image pin. */
function synthesize(
  queryLatencyThresholdMs?: number,
  controlledEnvironment?: Readonly<Record<string, string | undefined>>,
  imageDigest?: string,
): Template {
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
    imageDigest,
    tracing: false,
    queryLatencyThresholdMs,
    controlledEnvironment,
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

test('symptom descriptions expose only neutral coordinates without changing metric dimensions', () => {
  const alarms = Object.values(
    synthesize().findResources('AWS::CloudWatch::Alarm'),
  ) as CfnResource[];
  const symptoms = alarms.filter((alarm) =>
    ['VitalIngestFailures', 'PatientVitalsQueryDuration'].includes(
      alarm.Properties?.MetricName as string,
    ),
  );
  expect(symptoms).toHaveLength(2);
  for (const alarm of symptoms) {
    expect(alarm.Properties?.Dimensions).toEqual([
      { Name: 'ServiceName', Value: 'healthcare-sensor-app' },
    ]);
    expect(alarm.Properties?.AlarmDescription).toContain(
      'Resources: LogGroup=/ecs/RcaAgentDev/healthcare; ' +
        'ECSCluster=RcaAgentDevHealthcare; ECSService=RcaAgentDevHealthcare; ' +
        'RDSInstance=rcaagentdev-postgres.',
    );
    expect(alarm.Properties?.AlarmDescription).not.toMatch(
      /maintenance|demo|blocker|lock|run.?id|stop.?task|root.?cause/i,
    );
  }
  synthesize().hasResourceProperties('AWS::CloudWatch::Alarm', {
    MetricName: 'VitalIngestFailures',
    Statistic: 'Sum',
    Period: 60,
    EvaluationPeriods: 2,
    Threshold: 1,
    ComparisonOperator: 'GreaterThanOrEqualToThreshold',
    TreatMissingData: 'notBreaching',
  });
});

test('healthy workload controls are explicit and cannot select a fault', () => {
  synthesize(500, {
    TRAFFIC_ENABLED: 'true',
    TRAFFIC_INTERVAL_SECONDS: '0.5',
    TRAFFIC_MAX_CONCURRENCY: '4',
    TRAFFIC_QUERY_LIMIT: '100',
    TRAFFIC_PATIENT_ID: 'patient-1',
    TRAFFIC_SEED: '42',
    DB_POOL_TIMEOUT_SECONDS: '5',
    DB_STATEMENT_TIMEOUT_MS: '2000',
    DB_OBSERVABILITY_ENABLED: 'true',
    DB_OBSERVABILITY_INTERVAL_SECONDS: '5',
  }).hasResourceProperties('AWS::ECS::TaskDefinition', {
    ContainerDefinitions: Match.arrayWith([
      Match.objectLike({
        Environment: Match.arrayWith([
          { Name: 'FAULT_DB_LEAK', Value: 'false' },
          { Name: 'TRAFFIC_MAX_CONCURRENCY', Value: '4' },
          { Name: 'DB_POOL_TIMEOUT_SECONDS', Value: '5' },
        ]),
      }),
    ]),
  });
  for (const settings of [
    { FAULT_DB_LEAK: 'true' },
    { TRAFFIC_MAX_CONCURRENCY: '1.5' },
    { DB_POOL_TIMEOUT_SECONDS: '0' },
    { DB_STATEMENT_TIMEOUT_MS: '-1' },
    { TRAFFIC_ENABLED: 'yes' },
  ]) {
    expect(() => synthesize(500, settings)).toThrow();
  }
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

test('only symptom alarms publish state changes to the RCA topic', () => {
  const alarms = Object.values(
    synthesize().findResources('AWS::CloudWatch::Alarm'),
  ) as CfnResource[];
  const entryAlarmNames = [
    'RcaAgentDev-Healthcare-VitalIngestFailures',
    'RcaAgentDev-Healthcare-PatientVitalsQueryLatency',
  ];
  const alarmsWithActions = alarms.filter(
    (alarm) =>
      (alarm.Properties?.AlarmActions as unknown[] | undefined)?.length,
  );
  const alarmsWithOkActions = alarms.filter(
    (alarm) => (alarm.Properties?.OKActions as unknown[] | undefined)?.length,
  );

  expect(alarmsWithActions.map((alarm) => alarm.Properties?.AlarmName)).toEqual(
    entryAlarmNames,
  );
  expect(
    alarmsWithOkActions.map((alarm) => alarm.Properties?.AlarmName),
  ).toEqual(entryAlarmNames);

  const alarmActions = alarmsWithActions[0].Properties
    ?.AlarmActions as unknown[];
  const okActions = alarmsWithOkActions[0].Properties?.OKActions as unknown[];
  expect(alarmActions).toHaveLength(1);
  expect(okActions).toEqual(alarmActions);
  expect(alarmActions[0]).toEqual({
    'Fn::ImportValue': expect.stringContaining('AlarmTopic'),
  });
});

test('query latency uses the app metric unit and a configurable threshold', () => {
  for (const threshold of [500, 850]) {
    synthesize(threshold).hasResourceProperties('AWS::CloudWatch::Alarm', {
      MetricName: 'PatientVitalsQueryDuration',
      Namespace: 'Healthcare/Sensor',
      Dimensions: [{ Name: 'ServiceName', Value: 'healthcare-sensor-app' }],
      Unit: 'Milliseconds',
      Statistic: 'Average',
      Period: 60,
      EvaluationPeriods: 2,
      Threshold: threshold,
      TreatMissingData: 'missing',
    });
  }
  expect(
    alarmThreshold(synthesize(), 'RcaAgentDev-Healthcare-RdsHighConnections'),
  ).toBe(12);
  synthesize().hasResourceProperties('AWS::ECS::Service', {
    NetworkConfiguration: {
      AwsvpcConfiguration: Match.objectLike({ AssignPublicIp: 'DISABLED' }),
    },
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

test('opting into a digest changes only the application image', () => {
  const legacy = synthesize().toJSON();
  const digest = `sha256:${'a1'.repeat(32)}`;
  const pinned = synthesize(undefined, undefined, digest).toJSON();
  const task = Object.values(legacy.Resources).find(
    (resource) =>
      (resource as { Type: string }).Type === 'AWS::ECS::TaskDefinition',
  ) as { Properties: { ContainerDefinitions: { Image: unknown }[] } };
  const container = task.Properties.ContainerDefinitions[0];
  expect(JSON.stringify(container.Image)).toContain('/healthcare:latest');
  // Keep every other resource/property identical, including the revision label,
  // roles, network, alarms and workload settings.
  container.Image = JSON.parse(
    JSON.stringify(container.Image).replace(
      '/healthcare:latest',
      `/healthcare@${digest}`,
    ),
  );
  expect(pinned).toEqual(legacy);
});

test.each([
  '',
  'latest',
  `sha256:${'a'.repeat(63)}`,
  `sha256:${'a'.repeat(65)}`,
  `sha256:${'G'.repeat(64)}`,
  `sha256:${'A'.repeat(64)}`,
  `sha512:${'a'.repeat(64)}`,
  `repository@sha256:${'a'.repeat(64)}`,
  `sha256:${'a'.repeat(64)}\n`,
])('rejects a malformed direct stack digest: %j', (digest) => {
  expect(() => synthesize(undefined, undefined, digest)).toThrow(
    'healthcare.imageDigest',
  );
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
