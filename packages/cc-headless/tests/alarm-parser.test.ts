import { describe, it, expect } from 'vitest';
import { parseAlarmFromSqs } from '../src/alarm-parser.js';
import type { SQSEvent } from 'aws-lambda';

function makeSqsEvent(body: Record<string, unknown>): SQSEvent {
  return {
    Records: [
      {
        messageId: 'msg-1',
        receiptHandle: 'handle-1',
        body: JSON.stringify(body),
        attributes: {} as never,
        messageAttributes: {},
        md5OfBody: '',
        eventSource: 'aws:sqs',
        eventSourceARN: 'arn:aws:sqs:us-east-1:123456789012:queue',
        awsRegion: 'us-east-1',
      },
    ],
  };
}

describe('parseAlarmFromSqs', () => {
  it('parses raw CloudWatch alarm body', () => {
    const event = makeSqsEvent({
      AlarmName: 'HighCPU',
      NewStateValue: 'ALARM',
      NewStateReason: 'Threshold crossed',
      StateChangeTime: '2026-04-22T10:00:00Z',
      Region: 'ap-northeast-2',
      Trigger: {
        MetricName: 'CPUUtilization',
        Namespace: 'AWS/ECS',
        Dimensions: [{ name: 'ServiceName', value: 'web-service' }],
        Statistic: 'Average',
        Period: 300,
        Threshold: 90,
        ComparisonOperator: 'GreaterThanThreshold',
      },
    });

    const alarm = parseAlarmFromSqs(event);

    expect(alarm.alarmName).toBe('HighCPU');
    expect(alarm.stateReason).toBe('Threshold crossed');
    expect(alarm.region).toBe('ap-northeast-2');
    expect(alarm.metricName).toBe('CPUUtilization');
    expect(alarm.namespace).toBe('AWS/ECS');
    expect(alarm.dimensions).toEqual({ ServiceName: 'web-service' });
    expect(alarm.threshold).toBe(90);
  });

  it('parses SNS-wrapped alarm body', () => {
    const alarmData = {
      AlarmName: 'HighLatency',
      NewStateReason: 'p99 > 500ms',
      Region: 'us-east-1',
    };
    const event = makeSqsEvent({
      Message: JSON.stringify(alarmData),
      Type: 'Notification',
    });

    const alarm = parseAlarmFromSqs(event);

    expect(alarm.alarmName).toBe('HighLatency');
    expect(alarm.stateReason).toBe('p99 > 500ms');
  });

  it('handles missing fields gracefully', () => {
    const event = makeSqsEvent({});

    const alarm = parseAlarmFromSqs(event);

    expect(alarm.alarmName).toBe('UnknownAlarm');
    expect(alarm.stateReason).toBe('');
    expect(alarm.metricName).toBeUndefined();
    expect(alarm.dimensions).toBeUndefined();
  });
});
