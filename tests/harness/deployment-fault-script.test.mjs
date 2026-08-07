import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';

import { REPOSITORY_ROOT } from './evaluator.mjs';

const scriptPath = path.join(
  REPOSITORY_ROOT,
  'scripts/inject_deployment_fault.py',
);

test('deployment fault mutations require a caller-owned run id', async () => {
  const source = await readFile(scriptPath, 'utf8');

  assert.match(source, /--run-id is required for mutating actions/);
  assert.match(source, /"RCA_TEST_RUN_ID": run_id/);
  assert.match(source, /"runId": run_id/);
});

test('deployment fault status follows the service revision, not the latest family revision', async () => {
  const source = await readFile(scriptPath, 'utf8');

  assert.match(source, /def service_task_definition_arn\(/);
  assert.match(source, /"describe-services"/);
  assert.match(
    source,
    /"describe-task-definition",\s*"--task-definition",\s*service_task_definition_arn\(\)/,
  );
});

test('deployment and cleanup wait for stable infrastructure', async () => {
  const source = await readFile(scriptPath, 'utf8');

  assert.match(source, /"wait",\s*"services-stable"/);
  assert.match(source, /def wait_for_alarms_ok\(/);
  assert.match(source, /all\(latest\.get\(name\) == "OK"/);
  assert.match(source, /--restore-db-parameter-group/);
  assert.match(source, /--delete-db-parameter-group/);
  assert.match(
    source,
    /--delete-db-parameter-group requires --restore-db-parameter-group/,
  );
  assert.match(source, /refusing to delete the restored DB parameter group/);
});
