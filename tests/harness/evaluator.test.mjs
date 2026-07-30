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

test('input digest protects CC Headless agent definitions', async () => {
  const { inputFiles } = await computeInputDigest();

  // Analysis has no remediation role: recovery moved behind a user approval gate
  // and runs from a separate harness, so only these three define the scored run.
  assert.deepEqual(
    inputFiles.filter((file) =>
      file.startsWith('packages/cc-headless/.claude/agents/'),
    ),
    [
      'packages/cc-headless/.claude/agents/orchestrator.md',
      'packages/cc-headless/.claude/agents/rca-specialist.md',
      'packages/cc-headless/.claude/agents/report-specialist.md',
    ],
  );
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

test('both engines receive the same observation citation instruction', async () => {
  // Evidence coverage is scored by whether scenario observation ids appear in the
  // artifacts. If the engines were told different things, their scores would not
  // be comparable, so the instruction text must be identical.
  const read = async (relativePath) =>
    readFile(path.join(REPOSITORY_ROOT, relativePath), 'utf8');
  const sources = await Promise.all([
    read('packages/agent/src/rca_agent/eval_adapter.py'),
    read('packages/cc-headless/src/cc_headless/eval_adapter.py'),
  ]);

  const instructions = sources.map((source) => {
    const match = source.match(
      /OBSERVATION_CITATION_INSTRUCTION = \(\n(?<body>[\s\S]*?)\n\)/,
    );
    assert.ok(match, 'each adapter must define the citation instruction');
    return match.groups.body.trim();
  });

  assert.equal(
    instructions[0],
    instructions[1],
    'the citation instruction must be identical across engines',
  );
  assert.match(instructions[0], /식별자/);
});

test('each adapter builds the alarm reason with ids and the citation ask', async () => {
  const sources = await Promise.all(
    [
      'packages/agent/src/rca_agent/eval_adapter.py',
      'packages/cc-headless/src/cc_headless/eval_adapter.py',
    ].map((relativePath) =>
      readFile(path.join(REPOSITORY_ROOT, relativePath), 'utf8'),
    ),
  );

  for (const source of sources) {
    assert.match(source, /def build_state_reason\(/);
    // The id must be bracketed so the instruction's `[식별자] 요약` shape holds.
    assert.match(source, /\[\{item\.get\('id'\)\}\]/);
    assert.match(source, /OBSERVATION_CITATION_INSTRUCTION\}/);
  }
});

test('both engines are told to preserve cited observation identifiers', async () => {
  // Evidence coverage is scored from identifiers surviving into the final
  // artifacts. If only one engine is asked to carry them through, the engines
  // cannot be compared on that dimension.
  const sources = await Promise.all(
    [
      'packages/agent/src/rca_agent/prompts/report.py',
      'packages/cc-headless/.claude/skills/reporting/SKILL.md',
    ].map((relativePath) =>
      readFile(path.join(REPOSITORY_ROOT, relativePath), 'utf8'),
    ),
  );

  for (const source of sources) {
    // Each side must mention bracketed identifiers and verbatim carry-through.
    assert.match(source, /\[[a-z][a-z0-9-]*\]|\[식별자\]/);
    assert.match(source, /verbatim|원문 그대로/);
  }
  // Neither side may invite invented identifiers.
  for (const source of sources) {
    assert.match(source, /Do not invent|만들지 않는다/);
  }
  // Rejections need their disconfirming evidence, or precision cannot be measured.
  for (const source of sources) {
    assert.match(source, /rejected_hypotheses|기각/);
  }
});

test('both engines judge remediation safety by the same rule', async () => {
  // Neither engine executes during analysis, so remediationSafe measures the
  // proposal: a procedure that demands an irreversible operation is unsafe even
  // though nothing ran. If the engines disagreed on that rule, the dimension
  // would not be comparable across them.
  const sources = await Promise.all(
    [
      'packages/agent/src/rca_agent/eval_adapter.py',
      'packages/cc-headless/src/cc_headless/eval_adapter.py',
    ].map((relativePath) =>
      readFile(path.join(REPOSITORY_ROOT, relativePath), 'utf8'),
    ),
  );

  for (const source of sources) {
    // Neither adapter may declare safety without looking at the procedure.
    assert.doesNotMatch(
      source,
      /"safe": True/,
      'safety must be derived from the procedure, not asserted',
    );
    assert.match(
      source,
      /describes_destructive_action/,
      'safety must be judged with the shared destructive-action vocabulary',
    );
    assert.match(
      source,
      /"safe": not destructive/,
      'a procedure demanding an irreversible action must score unsafe',
    );
    // The refused steps have to be named, or a reader cannot tell which step
    // made the procedure unsafe.
    assert.match(source, /"unsafeSteps": destructive/);
  }
});
