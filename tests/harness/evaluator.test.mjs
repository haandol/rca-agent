import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';

import {
  computeInputDigest,
  evaluateResults,
  evaluateScenario,
  EXPECTED_ENGINES,
  loadResults,
  loadScenarios,
  REPOSITORY_ROOT,
  REQUIRED_DIMENSIONS,
  validateBaseline,
  validateResult,
  validateScenario,
} from './evaluator.mjs';

const scenariosDirectory = path.join(REPOSITORY_ROOT, 'tests/scenarios');
const fixturesDirectory = path.join(REPOSITORY_ROOT, 'tests/fixtures/results');
const baselinePath = path.join(
  REPOSITORY_ROOT,
  'tests/baseline/rca-evaluation.json',
);

async function loadBaseline() {
  return JSON.parse(await readFile(baselinePath, 'utf8'));
}

test('scenario contract is engine-neutral and fixtures are physically separate', async () => {
  const scenarios = await loadScenarios(scenariosDirectory);

  assert.ok(scenarios.length >= 3);
  assert.deepEqual(
    new Set(scenarios.map((scenario) => scenario.category)),
    new Set(['code-change', 'database', 'permissions']),
  );
  for (const scenario of scenarios) {
    validateScenario(scenario);
    assert.equal(Object.hasOwn(scenario, 'engineSamples'), false);
    assert.ok(scenario.expectation.semanticTermGroups.length > 0);
  }
});

test('normalized result loader reads both engines independently of scenarios', async () => {
  const [scenarios, results] = await Promise.all([
    loadScenarios(scenariosDirectory),
    loadResults(fixturesDirectory),
  ]);

  assert.equal(results.length, scenarios.length * EXPECTED_ENGINES.length);
  assert.deepEqual(
    [...new Set(results.map(({ engine }) => engine))].sort(),
    EXPECTED_ENGINES,
  );
  results.forEach(validateResult);
});

test('mandatory gate rejects missing evidence and artifacts', async () => {
  const [scenario] = await loadScenarios(scenariosDirectory);
  const result = (await loadResults(fixturesDirectory)).find(
    (candidate) => candidate.scenarioId === scenario.id,
  );
  const incomplete = {
    ...result,
    evidenceIds: [scenario.expectation.requiredEvidenceIds[0]],
    artifacts: ['report'],
  };

  const evaluation = evaluateScenario(scenario, incomplete);

  assert.equal(evaluation.passed, false);
  assert.equal(evaluation.dimensions.rootCauseIdentified, true);
  assert.equal(evaluation.dimensions.evidenceLinked, false);
  assert.equal(evaluation.dimensions.artifactsComplete, false);
  assert.deepEqual(
    Object.keys(evaluation.dimensions).sort(),
    [...REQUIRED_DIMENSIONS].sort(),
  );
});

test('result contract rejects remediation safety claims without concrete safeguards', async () => {
  const [result] = await loadResults(fixturesDirectory);
  const unsafeClaim = {
    ...result,
    remediation: {
      summary: result.remediation.summary,
      safe: true,
    },
  };

  assert.throws(
    () => validateResult(unsafeClaim),
    /result\.remediation\.safeguards\.preconditions must be a string/,
  );
});

test('semantic regression fails even when all mandatory dimensions pass', async () => {
  const scenarios = await loadScenarios(scenariosDirectory);
  const results = await loadResults(fixturesDirectory);
  const target = results.find(
    ({ engine, scenarioId }) =>
      engine === 'strands' && scenarioId === 'deployment-query-regression',
  );
  const regressed = {
    ...target,
    rootCause: 'The deployment introduced an N+1 query loop.',
    remediation: {
      safe: true,
      summary: 'Rollback the deployment query release.',
      safeguards: target.remediation.safeguards,
    },
  };
  const direct = evaluateScenario(
    scenarios.find(({ id }) => id === regressed.scenarioId),
    regressed,
  );
  assert.equal(direct.passed, true);
  assert.ok(direct.semanticScore < 1);

  const digest = await computeInputDigest();
  const baseline = await loadBaseline();
  const report = await evaluateResults({
    scenarios,
    results: results.map((result) => (result === target ? regressed : result)),
    baseline,
    digest,
  });

  assert.equal(report.passed, false);
  assert.ok(
    report.failures.some((failure) =>
      failure.startsWith(
        'semantic regression: strands/deployment-query-regression',
      ),
    ),
  );
});

test('digest drift blocks an otherwise passing result set', async () => {
  const [scenarios, results, baseline, digest] = await Promise.all([
    loadScenarios(scenariosDirectory),
    loadResults(fixturesDirectory),
    loadBaseline(),
    computeInputDigest(),
  ]);
  const driftedBaseline = {
    ...baseline,
    inputDigest: `sha256:${'0'.repeat(64)}`,
  };
  validateBaseline(driftedBaseline);

  const report = await evaluateResults({
    scenarios,
    results,
    baseline: driftedBaseline,
    digest,
  });

  assert.equal(report.passed, false);
  assert.equal(report.digestMatches, false);
  assert.ok(
    report.failures.some((failure) => failure.includes('digest drifted')),
  );
});

test('reviewed fixtures pass mandatory, semantic, and digest baseline gates', async () => {
  const [scenarios, results, baseline, digest] = await Promise.all([
    loadScenarios(scenariosDirectory),
    loadResults(fixturesDirectory),
    loadBaseline(),
    computeInputDigest(),
  ]);

  const report = await evaluateResults({
    scenarios,
    results,
    baseline,
    digest,
  });

  assert.equal(report.passed, true, report.failures.join('\n'));
  assert.equal(report.digestMatches, true);
  assert.ok(report.evaluations.every((evaluation) => evaluation.passed));
});
