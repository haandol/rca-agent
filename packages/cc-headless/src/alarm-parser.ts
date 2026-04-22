import type { SQSEvent } from 'aws-lambda';
import type { AlarmContext } from './prompt-builder.js';

interface CloudWatchAlarmSns {
  AlarmName?: string;
  NewStateValue?: string;
  NewStateReason?: string;
  StateChangeTime?: string;
  Region?: string;
  Trigger?: {
    MetricName?: string;
    Namespace?: string;
    Dimensions?: Array<{ name: string; value: string }>;
    Statistic?: string;
    Period?: number;
    Threshold?: number;
    ComparisonOperator?: string;
  };
}

function parseSnsEnvelope(body: string): CloudWatchAlarmSns {
  const parsed = JSON.parse(body);
  if (typeof parsed.Message === 'string') {
    return JSON.parse(parsed.Message);
  }
  return parsed;
}

function toDimensions(
  dims?: Array<{ name: string; value: string }>,
): Record<string, string> | undefined {
  if (!dims || dims.length === 0) return undefined;
  return Object.fromEntries(dims.map((d) => [d.name, d.value]));
}

export function parseAlarmFromSqs(event: SQSEvent): AlarmContext {
  const record = event.Records[0];
  const alarm = parseSnsEnvelope(record.body);

  return {
    alarmName: alarm.AlarmName ?? 'UnknownAlarm',
    stateReason: alarm.NewStateReason ?? '',
    stateChangeTime: alarm.StateChangeTime,
    region: alarm.Region ?? process.env.AWS_REGION ?? 'us-east-1',
    metricName: alarm.Trigger?.MetricName,
    namespace: alarm.Trigger?.Namespace,
    dimensions: toDimensions(alarm.Trigger?.Dimensions),
    statistic: alarm.Trigger?.Statistic,
    period: alarm.Trigger?.Period,
    threshold: alarm.Trigger?.Threshold,
    comparisonOperator: alarm.Trigger?.ComparisonOperator,
  };
}
