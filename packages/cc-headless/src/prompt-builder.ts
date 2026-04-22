import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SYSTEM_PROMPT_PATH = resolve(__dirname, '..', 'prompts', 'rca-system.md');
const USER_PROMPT_PATH = resolve(__dirname, '..', 'prompts', 'rca-user.md');

export interface AlarmContext {
  alarmName: string;
  stateReason: string;
  stateChangeTime?: string;
  region: string;
  metricName?: string;
  namespace?: string;
  dimensions?: Record<string, string>;
  statistic?: string;
  period?: number;
  threshold?: number;
  comparisonOperator?: string;
}

export function buildPrompt(alarm: AlarmContext): string {
  const systemPrompt = readFileSync(SYSTEM_PROMPT_PATH, 'utf-8');
  const userTemplate = readFileSync(USER_PROMPT_PATH, 'utf-8');

  const dimensionsStr = alarm.dimensions
    ? Object.entries(alarm.dimensions)
        .map(([k, v]) => `${k}=${v}`)
        .join(', ')
    : 'N/A';

  const userPrompt = userTemplate
    .replace('{alarm_name}', alarm.alarmName)
    .replace('{state_reason}', alarm.stateReason)
    .replace('{state_change_time}', alarm.stateChangeTime ?? 'N/A')
    .replace('{region}', alarm.region)
    .replace('{namespace}', alarm.namespace ?? 'N/A')
    .replace('{metric_name}', alarm.metricName ?? 'N/A')
    .replace('{dimensions}', dimensionsStr)
    .replace('{statistic}', alarm.statistic ?? 'Average')
    .replace('{period}', String(alarm.period ?? 300))
    .replace('{threshold}', String(alarm.threshold ?? 'N/A'))
    .replace('{comparison_operator}', alarm.comparisonOperator ?? 'N/A');

  return `${systemPrompt}\n\n---\n\n${userPrompt}`;
}
