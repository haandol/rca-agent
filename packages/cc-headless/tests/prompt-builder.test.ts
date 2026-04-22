import { describe, it, expect } from 'vitest';
import { buildPrompt, type AlarmContext } from '../src/prompt-builder.js';

describe('buildPrompt', () => {
  const alarm: AlarmContext = {
    alarmName: 'HighCPU',
    stateReason: 'Threshold crossed: 95 > 90',
    stateChangeTime: '2026-04-22T10:00:00Z',
    region: 'ap-northeast-2',
    metricName: 'CPUUtilization',
    namespace: 'AWS/ECS',
    dimensions: { ServiceName: 'web-service', ClusterName: 'prod' },
    statistic: 'Average',
    period: 300,
    threshold: 90,
    comparisonOperator: 'GreaterThanThreshold',
  };

  it('includes system prompt content', () => {
    const prompt = buildPrompt(alarm);

    expect(prompt).toContain('Root Cause Analysis');
    expect(prompt).toContain('Step 1: Initial Scoping');
    expect(prompt).toContain('Step 5: Report');
  });

  it('includes alarm details in user prompt', () => {
    const prompt = buildPrompt(alarm);

    expect(prompt).toContain('HighCPU');
    expect(prompt).toContain('Threshold crossed: 95 > 90');
    expect(prompt).toContain('ap-northeast-2');
    expect(prompt).toContain('AWS/ECS');
    expect(prompt).toContain('CPUUtilization');
  });

  it('formats dimensions correctly', () => {
    const prompt = buildPrompt(alarm);

    expect(prompt).toContain('ServiceName=web-service');
    expect(prompt).toContain('ClusterName=prod');
  });

  it('handles missing optional fields', () => {
    const minimal: AlarmContext = {
      alarmName: 'TestAlarm',
      stateReason: 'Test reason',
      region: 'us-east-1',
    };

    const prompt = buildPrompt(minimal);

    expect(prompt).toContain('TestAlarm');
    expect(prompt).toContain('N/A');
  });
});
