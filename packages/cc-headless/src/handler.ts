import { randomUUID } from 'node:crypto';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { SQSEvent, SQSHandler } from 'aws-lambda';
import { parseAlarmFromSqs } from './alarm-parser.js';
import { buildPrompt } from './prompt-builder.js';
import { runClaude } from './cc-runner.js';
import {
  checkDuplicate,
  createSession,
  updateState,
  markCompleted,
  markFailed,
} from './session-store.js';
import { saveReport, sendNotification } from './report-store.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const MCP_CONFIG_PATH = resolve(__dirname, '..', 'mcp-config.json');
const MAX_TURNS = 30;

export const handler: SQSHandler = async (event: SQSEvent) => {
  const startTime = Date.now();
  const alarm = parseAlarmFromSqs(event);
  const idempotencyKey = `${alarm.alarmName}#${alarm.stateChangeTime ?? 'unknown'}`;

  console.log(`Received alarm: ${alarm.alarmName}, key: ${idempotencyKey}`);

  if (await checkDuplicate(idempotencyKey)) {
    console.log(`Duplicate alarm, skipping: ${idempotencyKey}`);
    return;
  }

  const rcaId = randomUUID();
  const created = await createSession(rcaId, alarm.alarmName, idempotencyKey);
  if (!created) {
    console.log(`Session already exists for: ${idempotencyKey}`);
    return;
  }

  try {
    await updateState(rcaId, 'ANALYZING');

    const prompt = buildPrompt(alarm);
    console.log(`Starting CC headless analysis for RCA ${rcaId}`);

    const ccResult = await runClaude(prompt, {
      maxTurns: MAX_TURNS,
      mcpConfig: MCP_CONFIG_PATH,
    });

    const elapsedSeconds = Math.round((Date.now() - startTime) / 1000);

    if (!ccResult.success) {
      console.error(`CC headless failed: ${ccResult.result}`);
      await markFailed(rcaId, ccResult.result);
      return;
    }

    console.log(`CC headless completed in ${elapsedSeconds}s`);

    await updateState(rcaId, 'REPORT_GENERATION');

    const reportMarkdown = ccResult.result;
    const reportKey = await saveReport(rcaId, reportMarkdown);

    const rootCauseLine =
      reportMarkdown.match(/## Root Cause\n+(.*)/)?.[1] ??
      reportMarkdown.slice(0, 200);

    await markCompleted(rcaId, rootCauseLine);

    await sendNotification(
      rcaId,
      alarm.alarmName,
      rootCauseLine,
      reportKey,
      elapsedSeconds,
    );

    console.log(`RCA complete: rca_id=${rcaId}, elapsed=${elapsedSeconds}s`);
  } catch (err) {
    console.error(`Pipeline failed for ${alarm.alarmName}:`, err);
    await markFailed(
      rcaId,
      err instanceof Error ? err.message : 'Unknown error',
    );
  }
};
