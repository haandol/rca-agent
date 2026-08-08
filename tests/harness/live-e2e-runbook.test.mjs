import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';

import { REPOSITORY_ROOT } from './evaluator.mjs';

const runbookPath = path.join(
  REPOSITORY_ROOT,
  'docs/execution-live-e2e-runbook.md',
);

async function readRunbook() {
  return readFile(runbookPath, 'utf8');
}

test('live E2E runbook keeps fault injection inside the mandatory cleanup driver', async () => {
  const runbook = await readRunbook();

  assert.match(runbook, /scripts\/run_deployed_e2e\.py/);
  assert.match(runbook, /RCA_E2E_MANIFEST/);
  assert.match(runbook, /validation child/);
  assert.match(runbook, /d\["cleanup"\]\["result"\]\["clean"\] is True/);
  assert.match(runbook, /비정상 종료 복구/);
  assert.match(runbook, /ownedDbParameterGroupProofs/);
  assert.match(runbook, /parameterApplyStatus/);
});

test('live E2E runbook binds sessions to the exact post-fault ALARM transition', async () => {
  const runbook = await readRunbook();
  const scanStart = runbook.indexOf(
    'aws dynamodb scan --table-name RcaAgentDevRcaSession',
  );
  const scanEnd = runbook.indexOf('python3 - "$LIVE_SESSIONS_JSON"', scanStart);
  const scan = runbook.slice(scanStart, scanEnd);

  assert.notEqual(scanStart, -1);
  assert.notEqual(scanEnd, -1);
  assert.match(scan, /contains\(#sk, :s\)/);
  assert.match(scan, /#created >= :t/);
  assert.match(scan, /FAULT_COMPLETED_AT/);
  assert.doesNotMatch(scan, /alarm_name\s*=\s*:a/);
  assert.doesNotMatch(runbook, /first_state_change = min/);
  assert.doesNotMatch(runbook, /TEST_STARTED_AT/);
  assert.match(runbook, /expected exactly one symptom ALARM transition/);
  assert.match(runbook, /row\["state_change_time"\] == expected_state_change/);
  assert.match(runbook, /primary lineage must contain exactly two sessions/);
  assert.match(runbook, /"strands#SESSION"/);
  assert.match(runbook, /"cc-headless#SESSION"/);
  assert.match(
    runbook,
    /sessions must be COMPLETED before evidence inspection/,
  );
  assert.match(runbook, /must record report_s3_key/);
  assert.match(runbook, /causal alarm created forbidden RCA sessions/);
  assert.match(runbook, /ADDITIONAL_SYMPTOM_ALARM_PARTITION/);
});

test('live E2E runbook verifies RUN_ID and task revision lineage', async () => {
  const runbook = await readRunbook();

  assert.match(runbook, /red\["runId"\] == fault\["runId"\] == run_id/);
  assert.match(
    runbook,
    /red\["taskDefinitionArn"\] != fault\["taskDefinitionArn"\]/,
  );
  assert.match(runbook, /AttributeValue=RegisterTaskDefinition/);
  assert.match(runbook, /AttributeValue=UpdateService/);
  assert.match(runbook, /def exact_matches/);
  assert.match(runbook, /expected one exact RegisterTaskDefinition event/);
  assert.match(runbook, /expected one exact UpdateService event/);
  assert.match(runbook, /deployment\["startedAt"\]/);
  assert.match(runbook, /deployment\["completedAt"\]/);
  assert.match(runbook, /RCA_TEST_RUN_ID/);
  assert.match(runbook, /RCA_TEST_PHASE/);
  assert.match(runbook, /DEPLOYED_REVISION/);
});

test('live E2E runbook requires source evidence and rejects model-eval labels as proof', async () => {
  const runbook = await readRunbook();

  for (const required of [
    'VitalIngestFailures',
    'VitalIngestAttempts',
    'DatabaseConnections',
    'RcaAgentDev-Healthcare-RdsHighConnections',
    'DB session not returned to the pool',
    'database_adapter.py',
    'LOG_LEVEL',
    'unrelated-log-level-deployment',
  ]) {
    assert.ok(runbook.includes(required), `runbook must cover ${required}`);
  }
  assert.match(runbook, /model-eval/);
  assert.match(runbook, /live CloudWatch\/CloudTrail\/log\/code discovery/);
  assert.match(runbook, /세션이 기록한 정확한 `report_s3_key`/);
  assert.match(runbook, /원인에서\s*\n?\s*명시적으로 제외/);
});
