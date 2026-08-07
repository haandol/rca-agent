import assert from 'node:assert/strict';
import { mkdtemp, readFile, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';

import { approveBaseline } from './approve-cli.mjs';
import { readJsonFile } from './cli-utils.mjs';
import {
  EXPECTED_ENGINES,
  loadScenarios,
  REPOSITORY_ROOT,
  validateBaseline,
} from './evaluator.mjs';
import { runModelEvaluation, validateModelEnvironment } from './model-cli.mjs';

const fixturesDirectory = path.join(REPOSITORY_ROOT, 'tests/fixtures/results');
const fakeEnginePath = path.join(
  REPOSITORY_ROOT,
  'tests/fixtures/fake-engine.mjs',
);

test('model evaluation fails with actionable missing command errors', () => {
  assert.throws(
    () => validateModelEnvironment({ AWS_REGION: 'ap-northeast-2' }),
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

test('fake model commands run both engines and write normalized results and report', async () => {
  const directory = await mkdtemp(path.join(tmpdir(), 'rca-model-'));
  const baselinePath = path.join(directory, 'baseline.json');
  const resultsDirectory = path.join(directory, 'model-results');
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
  const outcome = await runModelEvaluation({
    env,
    baselinePath,
    resultsDirectory,
    reportPath,
    timeoutMs: 10_000,
  });

  assert.equal(outcome.report.passed, true, outcome.report.failures.join('\n'));
  assert.equal(outcome.report.executionMode, 'model-eval');
  // Every scenario is run against every engine, so a new scenario has to appear
  // on both sides rather than being scored for one engine only.
  const scenarioCount = (
    await loadScenarios(path.join(REPOSITORY_ROOT, 'tests/scenarios'))
  ).filter(({ executionModes }) =>
    executionModes.includes('model-eval'),
  ).length;
  assert.equal(
    outcome.report.evaluations.length,
    scenarioCount * EXPECTED_ENGINES.length,
  );
  assert.deepEqual(
    JSON.parse(await readFile(reportPath, 'utf8')),
    outcome.report,
  );
  for (const engine of EXPECTED_ENGINES) {
    const engineDirectory = path.join(resultsDirectory, engine);
    assert.equal((await stat(engineDirectory)).isDirectory(), true);
  }
});
