import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

test('root backlog freezes the complete controlled range and requires actual error membership', () => {
  const path = fileURLToPath(
    new URL('./vital_backlog_cases.py', import.meta.url),
  );
  const result = spawnSync('python3', [path, '-v'], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stdout + result.stderr);
});

test('default ECS probe reconciles stopped tasks and bounds owned cleanup without AWS calls', () => {
  const path = fileURLToPath(
    new URL('./vital_probe_cases.py', import.meta.url),
  );
  const result = spawnSync('python3', [path, '-v'], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stdout + result.stderr);
});
