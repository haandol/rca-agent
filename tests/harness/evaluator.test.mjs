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
  ROOT_FAULT_TYPES,
  SCENARIO_EXECUTION_MODES,
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
    assert.equal(scenario.expectation.requireConfirmedRootCause, true);
    assert.equal(
      typeof scenario.expectation.requireExecutableRemediation,
      'boolean',
    );
    assert.ok(scenario.expectation.acceptedRootFaultTypes.length > 0);
    assert.ok(
      scenario.expectation.acceptedRootFaultTypes.every((faultType) =>
        ROOT_FAULT_TYPES.includes(faultType),
      ),
    );
    for (const legacyField of [
      'rootCauseTermGroups',
      'remediationTermGroups',
      'semanticTermGroups',
    ]) {
      assert.equal(Object.hasOwn(scenario.expectation, legacyField), false);
    }
    assert.ok(scenario.executionModes.includes('model-eval'));
    assert.ok(
      scenario.executionModes.every((mode) =>
        SCENARIO_EXECUTION_MODES.includes(mode),
      ),
    );
  }
});

test('only scenarios backed by a deployed fault advertise deployed E2E', async () => {
  const scenarios = await loadScenarios(scenariosDirectory);
  const deployed = scenarios
    .filter(({ executionModes }) => executionModes.includes('deployed-e2e'))
    .map(({ id }) => id);

  assert.deepEqual(deployed, ['deployed-connection-leak-vital-ingest']);
});

test('scenario root and competing-cause evidence must be unambiguous observations', async () => {
  const scenario = (await loadScenarios(scenariosDirectory)).find(
    ({ id }) => id === 'deployed-connection-leak-vital-ingest',
  );
  const unknownRootEvidence = structuredClone(scenario);
  unknownRootEvidence.expectation.requiredRootCauseEvidenceIds = [
    'unknown-root-evidence',
  ];
  assert.throws(
    () => validateScenario(unknownRootEvidence),
    /required root-cause evidence ids must reference scenario observations/,
  );

  const rootEvidenceOutsideGlobal = structuredClone(scenario);
  rootEvidenceOutsideGlobal.expectation.requiredEvidenceIds =
    rootEvidenceOutsideGlobal.expectation.requiredEvidenceIds.filter(
      (id) =>
        id !==
        rootEvidenceOutsideGlobal.expectation.requiredRootCauseEvidenceIds[0],
    );
  assert.throws(
    () => validateScenario(rootEvidenceOutsideGlobal),
    /requiredRootCauseEvidenceIds must be a subset of requiredEvidenceIds/,
  );

  const causeEvidenceOutsideGlobal = structuredClone(scenario);
  causeEvidenceOutsideGlobal.expectation.requiredEvidenceIds =
    causeEvidenceOutsideGlobal.expectation.requiredEvidenceIds.filter(
      (id) => id !== 'unrelated-log-level-deployment',
    );
  assert.throws(
    () => validateScenario(causeEvidenceOutsideGlobal),
    /competingCauses\[2\]\.requiredEvidenceIds must be a subset of scenario\.expectation\.requiredEvidenceIds/,
  );

  const unknownEvidence = structuredClone(scenario);
  unknownEvidence.expectation.competingCauses[2].requiredEvidenceIds = [
    'unknown-red-herring',
  ];

  assert.throws(
    () => validateScenario(unknownEvidence),
    /requiredEvidenceIds must reference scenario observations/,
  );

  const duplicateCause = structuredClone(scenario);
  duplicateCause.expectation.competingCauses[2].id =
    duplicateCause.expectation.competingCauses[0].id;
  assert.throws(
    () => validateScenario(duplicateCause),
    /scenario competing cause ids must be unique/,
  );

  const overlappingEvidence = structuredClone(scenario);
  overlappingEvidence.expectation.competingCauses[1].requiredEvidenceIds.push(
    overlappingEvidence.expectation.competingCauses[0].requiredEvidenceIds[0],
  );
  assert.throws(
    () => validateScenario(overlappingEvidence),
    /requiredEvidenceIds must be pairwise disjoint/,
  );

  const proseTerms = structuredClone(scenario);
  proseTerms.expectation.competingCauses[0].terms = ['ignored prose'];
  assert.throws(
    () => validateScenario(proseTerms),
    /must contain only id and requiredEvidenceIds/,
  );
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

test('normalized result root-cause evidence must be globally cited', async () => {
  const result = (await loadResults(fixturesDirectory)).find(
    ({ engine, scenarioId }) =>
      engine === 'strands' && scenarioId === 'deployment-query-regression',
  );
  const missingRootCitation = {
    ...result,
    evidenceIds: result.evidenceIds.filter(
      (id) => id !== result.rootCauseEvidenceIds[0],
    ),
  };

  assert.throws(
    () => validateResult(missingRootCitation),
    /rootCauseEvidenceIds must be a subset of result\.evidenceIds/,
  );
});

test('normalized result competing-cause evidence must be globally cited', async () => {
  const result = (await loadResults(fixturesDirectory)).find(
    ({ engine, scenarioId }) =>
      engine === 'strands' &&
      scenarioId === 'deployed-connection-leak-vital-ingest',
  );
  const missingJudgmentCitation = {
    ...result,
    evidenceIds: result.evidenceIds.filter(
      (id) => id !== 'unrelated-log-level-deployment',
    ),
  };

  assert.throws(
    () => validateResult(missingJudgmentCitation),
    /competingCauseJudgments\[2\]\.evidenceIds must be a subset of result\.evidenceIds/,
  );
});

test('input digest protects Codex Headless agent definitions', async () => {
  const { inputFiles } = await computeInputDigest();

  // Analysis has no remediation role: recovery moved behind a user approval gate.
  // The digest covers both specialist instructions and their MCP capability files.
  assert.deepEqual(
    inputFiles.filter((file) =>
      file.startsWith('packages/codex-headless/harness/analysis/agents/'),
    ),
    [
      'packages/codex-headless/harness/analysis/agents/rca-specialist-model-eval.toml',
      'packages/codex-headless/harness/analysis/agents/rca-specialist.md',
      'packages/codex-headless/harness/analysis/agents/rca-specialist.toml',
      'packages/codex-headless/harness/analysis/agents/report-specialist-model-eval.toml',
      'packages/codex-headless/harness/analysis/agents/report-specialist.md',
      'packages/codex-headless/harness/analysis/agents/report-specialist.toml',
    ],
  );
  assert.ok(
    inputFiles.includes('packages/codex-headless/harness/analysis/AGENTS.md'),
  );
});

test('mandatory gate rejects missing evidence and artifacts', async () => {
  const [baseScenario] = await loadScenarios(scenariosDirectory);
  const scenario = structuredClone(baseScenario);
  scenario.observations.push({
    id: 'additional-global-evidence',
    source: 'logs',
    summary: 'Additional incident context required by the cloned scenario.',
  });
  scenario.expectation.requiredEvidenceIds.push('additional-global-evidence');
  const result = (await loadResults(fixturesDirectory)).find(
    (candidate) => candidate.scenarioId === scenario.id,
  );
  const incomplete = {
    ...result,
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

test('arbitrary root-cause and remediation prose passes with correct structure', async () => {
  const scenario = (await loadScenarios(scenariosDirectory)).find(
    ({ id }) => id === 'deployment-query-regression',
  );
  const result = (await loadResults(fixturesDirectory)).find(
    ({ engine, scenarioId }) =>
      engine === 'strands' && scenarioId === scenario.id,
  );

  const evaluation = evaluateScenario(scenario, {
    ...result,
    rootCause: 'An entirely different human explanation remains acceptable.',
    remediation: {
      ...result.remediation,
      summary: 'A free-form operator note can describe the proposed response.',
    },
  });

  assert.equal(evaluation.passed, true);
  assert.deepEqual(Object.keys(evaluation).sort(), ['dimensions', 'passed']);
});

test('keyword-rich prose cannot compensate for a wrong fault type or root evidence', async () => {
  const scenario = (await loadScenarios(scenariosDirectory)).find(
    ({ id }) => id === 'deployment-query-regression',
  );
  const result = (await loadResults(fixturesDirectory)).find(
    ({ engine, scenarioId }) =>
      engine === 'strands' && scenarioId === scenario.id,
  );
  const proseOnly = {
    rootCause:
      'N+1 per-sensor query loop deployment code change release slow query.',
    remediation: {
      ...result.remediation,
      summary: 'Rollback revert batch query deployment release.',
    },
  };
  for (const structuralError of [
    { rootFaultType: 'high-cpu' },
    { rootCauseEvidenceIds: [] },
  ]) {
    const evaluation = evaluateScenario(scenario, {
      ...result,
      ...proseOnly,
      ...structuralError,
    });
    assert.equal(evaluation.passed, false);
    assert.equal(evaluation.dimensions.rootCauseIdentified, false);
  }
});

test('root-cause evidence is evaluated separately from global evidence', async () => {
  const scenario = (await loadScenarios(scenariosDirectory)).find(
    ({ id }) => id === 'deployment-query-regression',
  );
  const result = (await loadResults(fixturesDirectory)).find(
    ({ engine, scenarioId }) =>
      engine === 'strands' && scenarioId === scenario.id,
  );
  const evaluation = evaluateScenario(scenario, {
    ...result,
    rootCauseEvidenceIds: [],
    evidenceIds: scenario.expectation.requiredEvidenceIds,
  });

  assert.equal(evaluation.dimensions.rootCauseIdentified, false);
  assert.equal(evaluation.dimensions.evidenceLinked, true);
  assert.equal(evaluation.passed, false);
});

test('mandatory gate rejects an explicitly unconfirmed structural root cause', async () => {
  const [scenario] = await loadScenarios(scenariosDirectory);
  const result = (await loadResults(fixturesDirectory)).find(
    (candidate) => candidate.scenarioId === scenario.id,
  );

  const evaluation = evaluateScenario(scenario, {
    ...result,
    rootCauseConfirmed: false,
  });

  assert.equal(evaluation.passed, false);
  assert.equal(evaluation.dimensions.rootCauseIdentified, false);
});

test('competing causes require exact rejected judgments with per-cause evidence', async () => {
  const scenario = (await loadScenarios(scenariosDirectory)).find(
    ({ id }) => id === 'deployed-connection-leak-vital-ingest',
  );
  const results = await loadResults(fixturesDirectory);
  const result = results.find(
    ({ engine, scenarioId }) =>
      engine === 'strands' && scenarioId === scenario.id,
  );

  const inconclusiveResult = {
    ...result,
    competingCauseJudgments: result.competingCauseJudgments.map((judgment) =>
      judgment.causeId === 'log-level-deployment'
        ? { ...judgment, judgment: 'inconclusive', evidenceIds: [] }
        : judgment,
    ),
  };
  assert.doesNotThrow(() => validateResult(inconclusiveResult));

  const inconclusive = evaluateScenario(scenario, inconclusiveResult);
  assert.equal(inconclusive.dimensions.competingCausesRejected, false);

  const explicitlyRejected = evaluateScenario(scenario, result);
  assert.equal(explicitlyRejected.dimensions.competingCausesRejected, true);

  const extraCause = evaluateScenario(scenario, {
    ...result,
    competingCauseJudgments: [
      ...result.competingCauseJudgments,
      {
        causeId: 'unexpected-cause',
        judgment: 'rejected',
        rationale: 'The recorded judgment is not part of the scenario.',
        evidenceIds: [],
      },
    ],
  });
  assert.equal(extraCause.dimensions.competingCausesRejected, false);
});

test('competing-cause evidence cannot receive aggregate cross-credit', async () => {
  const scenario = (await loadScenarios(scenariosDirectory)).find(
    ({ id }) => id === 'deployed-connection-leak-vital-ingest',
  );
  const result = (await loadResults(fixturesDirectory)).find(
    ({ engine, scenarioId }) =>
      engine === 'strands' && scenarioId === scenario.id,
  );
  const rotatedEvidence = result.competingCauseJudgments.map(
    (judgment, index, judgments) => ({
      ...judgment,
      evidenceIds: judgments[(index + 1) % judgments.length].evidenceIds,
    }),
  );
  const evaluation = evaluateScenario(scenario, {
    ...result,
    competingCauseJudgments: rotatedEvidence,
  });

  assert.deepEqual(
    new Set(rotatedEvidence.flatMap(({ evidenceIds }) => evidenceIds)),
    new Set(
      scenario.expectation.competingCauses.flatMap(
        ({ requiredEvidenceIds }) => requiredEvidenceIds,
      ),
    ),
  );
  assert.equal(evaluation.dimensions.competingCausesRejected, false);
});

test('result contract requires complete remediation structure and unique step ids', async () => {
  const result = (await loadResults(fixturesDirectory)).find(
    ({ engine, scenarioId }) =>
      engine === 'strands' &&
      scenarioId === 'deployed-connection-leak-vital-ingest',
  );
  const missingAvailability = {
    ...result,
    remediation: { ...result.remediation },
  };
  delete missingAvailability.remediation.available;
  assert.throws(
    () => validateResult(missingAvailability),
    /result\.remediation\.available must be a boolean/,
  );

  const duplicateStep = {
    ...result,
    remediation: {
      ...result.remediation,
      executionSteps: [
        result.remediation.executionSteps[0],
        result.remediation.executionSteps[0],
      ],
    },
  };
  assert.throws(
    () => validateResult(duplicateStep),
    /must have unique stepIds/,
  );

  const blankCriterion = structuredClone(result);
  blankCriterion.remediation.executionSteps[0].successCriteria = ' ';
  assert.throws(
    () => validateResult(blankCriterion),
    /successCriteria must not be empty/,
  );

  const blankSafeguard = structuredClone(result);
  blankSafeguard.remediation.safeguards.approval = ' ';
  assert.throws(
    () => validateResult(blankSafeguard),
    /safeguards\.approval must not be empty/,
  );
});

test('remediation availability, safety, draft status, and executability fail closed', async () => {
  const scenario = (await loadScenarios(scenariosDirectory)).find(
    ({ id }) => id === 'deployed-connection-leak-vital-ingest',
  );
  const result = (await loadResults(fixturesDirectory)).find(
    ({ engine, scenarioId }) =>
      engine === 'strands' && scenarioId === scenario.id,
  );
  const variants = [
    { ...result.remediation, available: false },
    { ...result.remediation, safe: false },
    { ...result.remediation, unsafeSteps: ['restore-healthy-revision'] },
    { ...result.remediation, verificationStatus: 'VERIFIED' },
    { ...result.remediation, executionSteps: [] },
  ];

  for (const remediation of variants) {
    const evaluation = evaluateScenario(scenario, {
      ...result,
      remediation,
    });
    assert.equal(evaluation.dimensions.remediationSafe, false);
    assert.equal(evaluation.passed, false);
  }

  const nonExecutableScenario = (await loadScenarios(scenariosDirectory)).find(
    ({ id }) => id === 'iam-policy-access-denied',
  );
  const nonExecutableResult = (await loadResults(fixturesDirectory)).find(
    ({ engine, scenarioId }) =>
      engine === 'strands' && scenarioId === nonExecutableScenario.id,
  );
  assert.equal(nonExecutableResult.remediation.executionSteps.length, 0);
  assert.equal(
    evaluateScenario(nonExecutableScenario, nonExecutableResult).dimensions
      .remediationSafe,
    true,
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

test('fixtures and approved baseline pass structural and digest gates', async () => {
  const [scenarios, results, baseline, digest] = await Promise.all([
    loadScenarios(scenariosDirectory),
    loadResults(fixturesDirectory),
    loadBaseline(),
    computeInputDigest(),
  ]);

  const mandatoryReport = await evaluateResults({
    scenarios,
    results,
    digest,
  });
  assert.equal(
    mandatoryReport.passed,
    true,
    mandatoryReport.failures.join('\n'),
  );
  assert.ok(
    mandatoryReport.evaluations.every((evaluation) => evaluation.passed),
  );
  assert.equal(mandatoryReport.schemaVersion, 2);
  assert.ok(
    mandatoryReport.evaluations.every(
      (evaluation) =>
        JSON.stringify(Object.keys(evaluation).sort()) ===
        JSON.stringify(['dimensions', 'engine', 'passed', 'scenarioId']),
    ),
  );

  const baselineReport = await evaluateResults({
    scenarios,
    results,
    baseline,
    digest,
  });

  assert.equal(baselineReport.passed, true, baselineReport.failures.join('\n'));
  assert.equal(baselineReport.digestMatches, true);
  assert.deepEqual(baselineReport.failures, []);
});

test('both engines receive the same observation citation instruction', async () => {
  // Evidence coverage is scored by whether scenario observation ids appear in the
  // artifacts. If the engines were told different things, their scores would not
  // be comparable, so the instruction text must be identical.
  const read = async (relativePath) =>
    readFile(path.join(REPOSITORY_ROOT, relativePath), 'utf8');
  const sources = await Promise.all([
    read('packages/agent/src/rca_agent/eval_adapter.py'),
    read('packages/codex-headless/src/codex_headless/eval_adapter.py'),
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
  assert.match(instructions[0], /대안 원인/);
  assert.match(instructions[0], /`rejected`/);
  assert.match(instructions[0], /같은 판정.*식별자/);
});

test('each adapter builds the alarm reason with ids and the citation ask', async () => {
  const sources = await Promise.all(
    [
      'packages/agent/src/rca_agent/eval_adapter.py',
      'packages/codex-headless/src/codex_headless/eval_adapter.py',
    ].map((relativePath) =>
      readFile(path.join(REPOSITORY_ROOT, relativePath), 'utf8'),
    ),
  );

  for (const source of sources) {
    const functionStart = source.indexOf('def build_state_reason(');
    assert.notEqual(
      functionStart,
      -1,
      'each adapter must build the alarm reason',
    );
    const nextFunction = source.indexOf('\n\ndef ', functionStart + 1);
    const functionBody = source.slice(
      functionStart,
      nextFunction === -1 ? undefined : nextFunction,
    );
    // The id must be bracketed so the instruction's `[식별자] 요약` shape holds.
    assert.match(source, /\[\{item\.get\('id'\)\}\]/);
    assert.match(functionBody, /OBSERVATION_CITATION_INSTRUCTION/);
  }
});

test('both engines are told to preserve cited observation identifiers', async () => {
  // Evidence coverage is scored from identifiers surviving into the final
  // artifacts. If only one engine is asked to carry them through, the engines
  // cannot be compared on that dimension.
  const sources = await Promise.all(
    [
      'packages/agent/src/rca_agent/prompts/report.py',
      'packages/codex-headless/harness/skills/reporting/SKILL.md',
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
      'packages/codex-headless/src/codex_headless/eval_adapter.py',
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
      /"safe": [^\n]*not destructive/,
      'a procedure demanding an irreversible action must score unsafe',
    );
    // The refused steps have to be named, or a reader cannot tell which step
    // made the procedure unsafe.
    assert.match(source, /"unsafeSteps": destructive/);
  }
});
