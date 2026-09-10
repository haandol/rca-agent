import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const cases = fileURLToPath(
  new URL('./realistic_demo_cases.py', import.meta.url),
);
const script = fileURLToPath(
  new URL('../../scripts/run_realistic_demo.py', import.meta.url),
);

test('realistic demo deterministic lifecycle and failure contracts', () => {
  const result = spawnSync('python3', [cases, '-v'], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stdout + result.stderr);
});

test('realistic demo CLI rejects incomplete controls without AWS calls', () => {
  for (const controls of [
    [
      '--scenario',
      'query-revision',
      '--image',
      'repo:latest',
      '--revision',
      'r2',
    ],
    [
      '--scenario',
      'session-revision',
      '--image',
      'repo:r3',
      '--revision',
      'r3',
    ],
    ['--scenario', 'pool-config', '--pool-size', '0'],
    ['--scenario', 'pool-config', '--hold-seconds', '7201'],
    ['--scenario', 'maintenance-lock', '--hold-seconds', 'nan'],
  ]) {
    const result = spawnSync(
      'python3',
      [
        script,
        'plan',
        '--run-id',
        'test',
        '--cluster',
        'c',
        '--service',
        's',
        '--region',
        'us-east-1',
        '--journal-root',
        '/unused',
        '--alarm',
        'a',
        '--alarm',
        'b',
        '--alarm',
        'c',
        ...controls,
      ],
      { encoding: 'utf8' },
    );
    assert.equal(result.status, 2, result.stdout + result.stderr);
    assert.match(result.stderr, /error:/);
  }
});

test('arbitrary maintenance argv is rejected before AWS access', () => {
  for (const action of ['plan', 'apply', 'status', 'restore']) {
    const result = spawnSync(
      'python3',
      [
        script,
        action,
        '--run-id',
        'test',
        '--cluster',
        'c',
        '--service',
        's',
        '--region',
        'us-east-1',
        '--journal-root',
        '/unused',
        '--maintenance-command-json',
        '["python", "-c", "pass", "{run_id}", "{hold_seconds}"]',
      ],
      { encoding: 'utf8' },
    );
    assert.equal(result.status, 2, result.stdout + result.stderr);
    assert.match(
      result.stderr,
      /unrecognized arguments: --maintenance-command-json/,
    );
  }
});

test('apply cannot replace the profile frozen by plan', () => {
  for (const override of [
    ['--expect-setting', 'TRAFFIC_QUERY_LIMIT=999'],
    ['--expect-setting', 'DB_OBSERVABILITY_ENABLED=false'],
    ['--pool-size', '2'],
    ['--image', `repo@sha256:${'b'.repeat(64)}`],
  ]) {
    const result = spawnSync(
      'python3',
      [
        script,
        'apply',
        '--run-id',
        'test',
        '--cluster',
        'c',
        '--service',
        's',
        '--region',
        'us-east-1',
        '--journal-root',
        '/unused',
        ...override,
      ],
      { encoding: 'utf8' },
    );
    assert.equal(result.status, 2, result.stdout + result.stderr);
    assert.match(result.stderr, /controls are frozen by plan/);
  }
});
