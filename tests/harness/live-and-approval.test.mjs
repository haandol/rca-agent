import assert from 'node:assert/strict';
import { mkdtemp, readFile, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';

import { approveBaseline } from './approve-cli.mjs';
import { readJsonFile } from './cli-utils.mjs';
import {
  EXPECTED_ENGINES,
  REPOSITORY_ROOT,
  validateBaseline,
} from './evaluator.mjs';
import { runLiveEvaluation, validateLiveEnvironment } from './live-cli.mjs';

const fixturesDirectory = path.join(REPOSITORY_ROOT, 'tests/fixtures/results');
const fakeEnginePath = path.join(
  REPOSITORY_ROOT,
  'tests/fixtures/fake-engine.mjs',
);

test('live evaluation fails with actionable missing command errors', () => {
  assert.throws(
    () => validateLiveEnvironment({ AWS_REGION: 'ap-northeast-2' }),
    /Missing RCA_EVAL_CC_HEADLESS_COMMAND/,
  );
});

test('approval writes a baseline only to the explicit destination', async () => {
  const directory = await mkdtemp(path.join(tmpdir(), 'rca-approval-'));
  const baselinePath = path.join(directory, 'approved.json');
  const baseline = await approveBaseline({
    resultsDirectory: fixturesDirectory,
    baselinePath,
    approvedAt: '2026-07-21T00:00:00.000Z',
  });

  validateBaseline(baseline);
  assert.deepEqual(
    await readJsonFile(baselinePath, 'approved baseline'),
    baseline,
  );
  assert.equal(baseline.approvedAt, '2026-07-21T00:00:00.000Z');
  assert.deepEqual(Object.keys(baseline.semanticScores), EXPECTED_ENGINES);
});

test('fake live commands run both engines and write normalized results and report', async () => {
  const directory = await mkdtemp(path.join(tmpdir(), 'rca-live-'));
  const baselinePath = path.join(directory, 'baseline.json');
  const resultsDirectory = path.join(directory, 'live-results');
  const reportPath = path.join(directory, 'report.json');
  await approveBaseline({
    resultsDirectory: fixturesDirectory,
    baselinePath,
    approvedAt: '2026-07-21T00:00:00.000Z',
  });

  const baseCommand = [process.execPath, fakeEnginePath];
  const env = {
    ...process.env,
    AWS_PROFILE: 'fake-test-profile',
    AWS_REGION: 'ap-northeast-2',
    RCA_EVAL_CC_HEADLESS_COMMAND: JSON.stringify([
      ...baseCommand,
      'cc-headless',
      '{scenario}',
    ]),
    RCA_EVAL_STRANDS_COMMAND: JSON.stringify([...baseCommand, 'strands']),
  };
  const outcome = await runLiveEvaluation({
    env,
    baselinePath,
    resultsDirectory,
    reportPath,
    timeoutMs: 10_000,
  });

  assert.equal(outcome.report.passed, true, outcome.report.failures.join('\n'));
  assert.equal(outcome.report.live, true);
  assert.equal(outcome.report.evaluations.length, 6);
  assert.deepEqual(
    JSON.parse(await readFile(reportPath, 'utf8')),
    outcome.report,
  );
  for (const engine of EXPECTED_ENGINES) {
    const engineDirectory = path.join(resultsDirectory, engine);
    assert.equal((await stat(engineDirectory)).isDirectory(), true);
  }
});
