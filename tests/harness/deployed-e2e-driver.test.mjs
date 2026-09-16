import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

test('deployed entry point shares the single run control without an approval or validation-child shortcut', async () => {
  const script = new URL('../../scripts/run_deployed_e2e.py', import.meta.url);
  const result = spawnSync('python3', [script.pathname, '--help'], {
    encoding: 'utf8',
  });
  assert.equal(result.status, 0);
  assert.match(result.stdout, /plan,apply,status,restore/);
  assert.match(result.stdout, /--journal-root/);
  assert.match(result.stdout, /--evidence-bucket/);
  const source = await readFile(script, 'utf8');
  assert.match(source, /3 preparation \+ 10 main/);
  assert.match(source, /run_realistic_demo.py/);
  assert.doesNotMatch(
    source,
    /approve_execution|requests\.post|subprocess\.Popen/,
  );
});
