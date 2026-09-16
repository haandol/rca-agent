import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import test from 'node:test';

test('legacy injector delegates to the single journaled CLI and rejects retired faults', () => {
  const script = new URL(
    '../../scripts/inject_deployment_fault.py',
    import.meta.url,
  ).pathname;
  const help = spawnSync('python3', [script, '--help'], { encoding: 'utf8' });
  assert.equal(help.status, 0);
  assert.match(help.stdout, /plan,apply,status,restore/);
  assert.match(help.stdout, /write-column-regression/);
  for (const action of [
    'db-leak',
    'red-herring',
    'cleanup',
    'maintenance-lock',
  ]) {
    const result = spawnSync('python3', [script, action], { encoding: 'utf8' });
    assert.equal(result.status, 2);
    assert.match(result.stderr, /invalid choice/);
  }
});
