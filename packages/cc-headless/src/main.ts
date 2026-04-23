import { randomUUID } from 'node:crypto';
import { createServer } from 'node:http';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  SQSClient,
  ReceiveMessageCommand,
  DeleteMessageCommand,
} from '@aws-sdk/client-sqs';
import { parseAlarm } from './alarm-parser.js';
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

const QUEUE_URL = process.env.SQS_QUEUE_URL ?? '';
const POLL_WAIT_SECONDS = parseInt(
  process.env.SQS_POLL_WAIT_SECONDS ?? '20',
  10,
);

let running = true;

function handleSignal(signal: string) {
  console.log(`Received ${signal}, shutting down`);
  running = false;
}

process.on('SIGTERM', () => handleSignal('SIGTERM'));
process.on('SIGINT', () => handleSignal('SIGINT'));

function startHealthServer(port = 8080) {
  const server = createServer((req, res) => {
    if (req.url === '/healthz') {
      res.writeHead(200, { 'Content-Type': 'text/plain' });
      res.end('ok');
    } else {
      res.writeHead(404);
      res.end();
    }
  });
  server.listen(port, '0.0.0.0');
  return server;
}

function parseSnsEnvelope(body: string): Record<string, unknown> {
  const parsed = JSON.parse(body);
  if (typeof parsed.Message === 'string') {
    return JSON.parse(parsed.Message);
  }
  return parsed;
}

async function processMessage(messageBody: string): Promise<void> {
  const startTime = Date.now();
  const alarmData = parseSnsEnvelope(messageBody);
  const alarm = parseAlarm(alarmData);
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
}

async function main(): Promise<void> {
  if (!QUEUE_URL) {
    console.error('SQS_QUEUE_URL is not set');
    process.exit(1);
  }

  const healthServer = startHealthServer();
  console.log('Health server started on port 8080');

  const sqs = new SQSClient({});
  console.log(`Starting SQS long polling: ${QUEUE_URL}`);

  while (running) {
    try {
      const resp = await sqs.send(
        new ReceiveMessageCommand({
          QueueUrl: QUEUE_URL,
          MaxNumberOfMessages: 1,
          WaitTimeSeconds: POLL_WAIT_SECONDS,
        }),
      );

      const messages = resp.Messages ?? [];
      if (messages.length === 0) continue;

      for (const msg of messages) {
        try {
          await processMessage(msg.Body ?? '{}');
        } catch (err) {
          console.error('Failed to process message:', err);
        } finally {
          await sqs.send(
            new DeleteMessageCommand({
              QueueUrl: QUEUE_URL,
              ReceiptHandle: msg.ReceiptHandle!,
            }),
          );
        }
      }
    } catch (err) {
      console.error('Failed to receive SQS message:', err);
      await new Promise((r) => setTimeout(r, 5000));
    }
  }

  healthServer.close();
  console.log('Shutdown complete');
}

main();
