import { describe, it, expect } from 'vitest';
import { parseAlarm } from '../src/alarm-parser.js';

describe('parseAlarm', () => {
  it('parses raw CloudWatch alarm body', () => {
    const alarm = parseAlarm({
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

    expect(alarm.alarmName).toBe('HighCPU');
    expect(alarm.stateReason).toBe('Threshold crossed');
    expect(alarm.region).toBe('ap-northeast-2');
    expect(alarm.metricName).toBe('CPUUtilization');
    expect(alarm.namespace).toBe('AWS/ECS');
    expect(alarm.dimensions).toEqual({ ServiceName: 'web-service' });
    expect(alarm.threshold).toBe(90);
  });

  it('handles missing fields gracefully', () => {
    const alarm = parseAlarm({});

    expect(alarm.alarmName).toBe('UnknownAlarm');
    expect(alarm.stateReason).toBe('');
    expect(alarm.metricName).toBeUndefined();
    expect(alarm.dimensions).toBeUndefined();
  });
});
