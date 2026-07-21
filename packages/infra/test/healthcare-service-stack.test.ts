import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as sns from 'aws-cdk-lib/aws-sns';
import { Template } from 'aws-cdk-lib/assertions';

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

test.failing('slow-query faults are covered by an RDS read-latency alarm', () => {
  expect(alarmMetricNames(synthesize())).toContain('ReadLatency');
});

test.failing('injected request latency is covered by a service p99 latency alarm', () => {
  const metricNames = alarmMetricNames(synthesize());

  expect(
    metricNames.some((metricName) =>
      ['RequestLatency', 'TargetResponseTime'].includes(metricName),
    ),
  ).toBe(true);
});
