import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readdir, readFile, stat } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const SLUG_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const CONTRACT_EXTENSIONS = new Set(['.json', '.md', '.py']);
export const SCENARIO_EXECUTION_MODES = ['deployed-e2e', 'model-eval'];
export const ROOT_FAULT_TYPES = [
  'db-leak',
  'high-cpu',
  'high-memory',
  'slow-query',
  'unsupported',
];

export const REPOSITORY_ROOT = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '../..',
);

export const REQUIRED_DIMENSIONS = [
  'rootCauseIdentified',
  'evidenceLinked',
  'artifactsComplete',
  'remediationSafe',
  'competingCausesRejected',
];

export const EXPECTED_ENGINES = ['cc-headless', 'strands'];

export const DEFAULT_CONTRACT_INPUTS = [
  'tests/harness/evaluator.mjs',
  'tests/fixtures/results',
  'packages/agent/src/rca_agent/prompts',
  'packages/agent/src/rca_agent/agent_factory.py',
  // Each model-eval adapter decides how a run becomes a normalized result, so
  // changing one changes the evaluated contract and requires re-approval.
  'packages/agent/src/rca_agent/eval_adapter.py',
  'packages/cc-headless/src/cc_headless/eval_adapter.py',
  'packages/cc-headless/CLAUDE.md',
  'packages/cc-headless/.claude/agents',
  'packages/cc-headless/prompts',
  'packages/cc-headless/.claude/skills',
  'packages/cc-headless/mcp-config.json',
  'packages/cc-headless/src/cc_headless/adapters/secondary/cc/cc_subprocess_runner.py',
  'packages/cc-headless/src/cc_headless/mcp_server.py',
  'packages/cc-headless/src/cc_headless/services/prompt_builder.py',
  // The adapters derive remediation safety from this vocabulary.
  'packages/agent/src/rca_agent/services/destructive_actions.py',
  'packages/cc-headless/src/cc_headless/services/destructive_actions.py',
];

function assertString(value, label) {
  assert.equal(typeof value, 'string', `${label} must be a string`);
  assert.ok(value.trim(), `${label} must not be empty`);
}

function assertStringArray(value, label, minimum = 1) {
  assert.ok(Array.isArray(value), `${label} must be an array`);
  assert.ok(
    value.length >= minimum,
    `${label} must contain ${minimum} item(s)`,
  );
  value.forEach((item, index) => assertString(item, `${label}[${index}]`));
  assert.equal(
    new Set(value).size,
    value.length,
    `${label} must not contain duplicates`,
  );
}

function assertCompetingCauses(causes, observationIds, requiredEvidenceIds) {
  assert.ok(
    Array.isArray(causes),
    'scenario.expectation.competingCauses must be an array',
  );
  causes.forEach((cause, index) => {
    const label = `scenario.expectation.competingCauses[${index}]`;
    assert.equal(typeof cause, 'object', `${label} must be an object`);
    assert.ok(cause, `${label} must not be null`);
    assert.deepEqual(
      Object.keys(cause).sort(),
      ['id', 'requiredEvidenceIds'],
      `${label} must contain only id and requiredEvidenceIds`,
    );
    assertString(cause.id, `${label}.id`);
    assert.match(
      cause.id,
      SLUG_PATTERN,
      `${label}.id must be a kebab-case slug`,
    );
    assertStringArray(
      cause.requiredEvidenceIds,
      `${label}.requiredEvidenceIds`,
    );
    assert.ok(
      cause.requiredEvidenceIds.every((id) => observationIds.has(id)),
      `${label}.requiredEvidenceIds must reference scenario observations`,
    );
    assert.ok(
      includesAll(requiredEvidenceIds, cause.requiredEvidenceIds),
      `${label}.requiredEvidenceIds must be a subset of scenario.expectation.requiredEvidenceIds`,
    );
  });
  assert.equal(
    new Set(causes.map(({ id }) => id)).size,
    causes.length,
    'scenario competing cause ids must be unique',
  );
  const competingCauseEvidenceIds = causes.flatMap(
    ({ requiredEvidenceIds: causeEvidenceIds }) => causeEvidenceIds,
  );
  assert.equal(
    new Set(competingCauseEvidenceIds).size,
    competingCauseEvidenceIds.length,
    'scenario competing cause requiredEvidenceIds must be pairwise disjoint',
  );
}

function includesAll(actual, expected) {
  const values = new Set(actual);
  return expected.every((value) => values.has(value));
}

async function collectFiles(entryPath) {
  const entryStat = await stat(entryPath);
  if (entryStat.isFile()) {
    return [entryPath];
  }

  const entries = await readdir(entryPath, { withFileTypes: true });
  const nested = await Promise.all(
    entries
      .filter(
        (entry) =>
          entry.isDirectory() ||
          (entry.isFile() && CONTRACT_EXTENSIONS.has(path.extname(entry.name))),
      )
      .map((entry) => collectFiles(path.join(entryPath, entry.name))),
  );
  return nested.flat();
}

async function readJson(filePath, label) {
  try {
    return JSON.parse(await readFile(filePath, 'utf8'));
  } catch (error) {
    throw new Error(`${label} is not valid JSON: ${filePath}`, {
      cause: error,
    });
  }
}

export function validateScenario(scenario) {
  assert.equal(typeof scenario, 'object', 'scenario must be an object');
  assert.ok(scenario, 'scenario must not be null');
  assertString(scenario.id, 'scenario.id');
  assert.match(
    scenario.id,
    SLUG_PATTERN,
    'scenario.id must be a kebab-case slug',
  );
  assertString(scenario.category, 'scenario.category');
  assertStringArray(scenario.executionModes, 'scenario.executionModes');
  assert.ok(
    scenario.executionModes.every((mode) =>
      SCENARIO_EXECUTION_MODES.includes(mode),
    ),
    `scenario.executionModes must contain only ${SCENARIO_EXECUTION_MODES.join(', ')}`,
  );
  assertString(scenario.alarm?.name, 'scenario.alarm.name');
  assertString(scenario.alarm?.metric, 'scenario.alarm.metric');
  assertString(scenario.alarm?.stateReason, 'scenario.alarm.stateReason');
  assert.ok(
    Array.isArray(scenario.observations) && scenario.observations.length >= 2,
    'scenario.observations must contain at least two observations',
  );
  scenario.observations.forEach((observation, index) => {
    assertString(observation.id, `scenario.observations[${index}].id`);
    assertString(observation.source, `scenario.observations[${index}].source`);
    assertString(
      observation.summary,
      `scenario.observations[${index}].summary`,
    );
  });
  assert.equal(
    new Set(scenario.observations.map(({ id }) => id)).size,
    scenario.observations.length,
    'scenario observation ids must be unique',
  );

  const expectation = scenario.expectation;
  assert.equal(
    typeof expectation,
    'object',
    'scenario.expectation is required',
  );
  assertStringArray(
    expectation.acceptedRootFaultTypes,
    'scenario.expectation.acceptedRootFaultTypes',
  );
  assert.ok(
    expectation.acceptedRootFaultTypes.every((faultType) =>
      ROOT_FAULT_TYPES.includes(faultType),
    ),
    `scenario.expectation.acceptedRootFaultTypes must contain only ${ROOT_FAULT_TYPES.join(', ')}`,
  );
  assertStringArray(
    expectation.requiredRootCauseEvidenceIds,
    'scenario.expectation.requiredRootCauseEvidenceIds',
  );
  assertStringArray(
    expectation.requiredEvidenceIds,
    'scenario.expectation.requiredEvidenceIds',
  );
  const observationIds = new Set(
    scenario.observations.map((observation) => observation.id),
  );
  assert.ok(
    expectation.requiredRootCauseEvidenceIds.every((id) =>
      observationIds.has(id),
    ),
    'required root-cause evidence ids must reference scenario observations',
  );
  assert.ok(
    expectation.requiredEvidenceIds.every((id) => observationIds.has(id)),
    'required evidence ids must reference scenario observations',
  );
  assert.ok(
    includesAll(
      expectation.requiredEvidenceIds,
      expectation.requiredRootCauseEvidenceIds,
    ),
    'requiredRootCauseEvidenceIds must be a subset of requiredEvidenceIds',
  );
  assertStringArray(
    expectation.requiredArtifacts,
    'scenario.expectation.requiredArtifacts',
  );
  assert.ok(
    expectation.requiredArtifacts.includes('report'),
    'required artifacts must include report',
  );
  assert.equal(
    typeof expectation.requireConfirmedRootCause,
    'boolean',
    'scenario.expectation.requireConfirmedRootCause must be a boolean',
  );
  assert.equal(
    typeof expectation.requireExecutableRemediation,
    'boolean',
    'scenario.expectation.requireExecutableRemediation must be a boolean',
  );
  assertCompetingCauses(
    expectation.competingCauses ?? [],
    observationIds,
    expectation.requiredEvidenceIds,
  );
  assert.equal(
    [
      'rootCauseTermGroups',
      'remediationTermGroups',
      'semanticTermGroups',
      'rejectedCauseTermGroups',
      'rejectedCauseEvidenceIds',
    ].some((field) => Object.hasOwn(expectation, field)),
    false,
    'legacy prose-term expectations are not supported',
  );
  assert.equal(
    Object.hasOwn(scenario, 'engineSamples'),
    false,
    'scenario fixtures must not contain engineSamples',
  );
}

export function validateResult(result) {
  assert.equal(typeof result, 'object', 'result must be an object');
  assert.ok(result, 'result must not be null');
  assert.equal(result.schemaVersion, 2, 'result.schemaVersion must be 2');
  assertString(result.scenarioId, 'result.scenarioId');
  assert.match(
    result.scenarioId,
    SLUG_PATTERN,
    'result.scenarioId must be a kebab-case slug',
  );
  assertString(result.engine, 'result.engine');
  assert.match(
    result.engine,
    SLUG_PATTERN,
    'result.engine must be a kebab-case slug',
  );
  assertString(result.rootCause, 'result.rootCause');
  assert.equal(
    typeof result.rootCauseConfirmed,
    'boolean',
    'result.rootCauseConfirmed must be a boolean',
  );
  assert.ok(
    ROOT_FAULT_TYPES.includes(result.rootFaultType),
    `result.rootFaultType must be one of ${ROOT_FAULT_TYPES.join(', ')}`,
  );
  assertStringArray(
    result.rootCauseEvidenceIds,
    'result.rootCauseEvidenceIds',
    0,
  );
  assertStringArray(result.evidenceIds, 'result.evidenceIds');
  assert.ok(
    includesAll(result.evidenceIds, result.rootCauseEvidenceIds),
    'result.rootCauseEvidenceIds must be a subset of result.evidenceIds',
  );
  assertStringArray(result.artifacts, 'result.artifacts');
  assert.ok(
    Array.isArray(result.competingCauseJudgments),
    'result.competingCauseJudgments must be an array',
  );
  result.competingCauseJudgments.forEach((judgment, index) => {
    const label = `result.competingCauseJudgments[${index}]`;
    assert.equal(typeof judgment, 'object', `${label} must be an object`);
    assert.ok(judgment, `${label} must not be null`);
    assertString(judgment.causeId, `${label}.causeId`);
    assert.match(
      judgment.causeId,
      SLUG_PATTERN,
      `${label}.causeId must be a kebab-case slug`,
    );
    assert.ok(
      ['rejected', 'not-rejected', 'inconclusive'].includes(judgment.judgment),
      `${label}.judgment must be rejected, not-rejected, or inconclusive`,
    );
    assertString(judgment.rationale, `${label}.rationale`);
    assertStringArray(judgment.evidenceIds, `${label}.evidenceIds`, 0);
    assert.ok(
      includesAll(result.evidenceIds, judgment.evidenceIds),
      `${label}.evidenceIds must be a subset of result.evidenceIds`,
    );
  });
  assert.equal(
    new Set(result.competingCauseJudgments.map(({ causeId }) => causeId)).size,
    result.competingCauseJudgments.length,
    'result competing cause judgments must have unique causeIds',
  );
  assertString(result.remediation?.summary, 'result.remediation.summary');
  assert.equal(
    typeof result.remediation?.available,
    'boolean',
    'result.remediation.available must be a boolean',
  );
  assertString(
    result.remediation?.verificationStatus,
    'result.remediation.verificationStatus',
  );
  assert.ok(
    Array.isArray(result.remediation?.executionSteps),
    'result.remediation.executionSteps must be an array',
  );
  result.remediation.executionSteps.forEach((step, index) => {
    const label = `result.remediation.executionSteps[${index}]`;
    assert.equal(typeof step, 'object', `${label} must be an object`);
    assert.ok(step, `${label} must not be null`);
    assert.deepEqual(
      Object.keys(step).sort(),
      ['action', 'intent', 'stepId', 'successCriteria'],
      `${label} must contain only stepId, intent, action, and successCriteria`,
    );
    assertString(step.stepId, `${label}.stepId`);
    assertString(step.intent, `${label}.intent`);
    assertString(step.action, `${label}.action`);
    assertString(step.successCriteria, `${label}.successCriteria`);
  });
  assert.equal(
    new Set(result.remediation.executionSteps.map(({ stepId }) => stepId)).size,
    result.remediation.executionSteps.length,
    'result.remediation.executionSteps must have unique stepIds',
  );
  assert.equal(
    typeof result.remediation?.safe,
    'boolean',
    'result.remediation.safe must be a boolean',
  );
  assertStringArray(
    result.remediation?.unsafeSteps,
    'result.remediation.unsafeSteps',
    0,
  );
  assertString(
    result.remediation?.safeguards?.preconditions,
    'result.remediation.safeguards.preconditions',
  );
  assertString(
    result.remediation?.safeguards?.approval,
    'result.remediation.safeguards.approval',
  );
  assertString(
    result.remediation?.safeguards?.rollback,
    'result.remediation.safeguards.rollback',
  );
  assertString(
    result.remediation?.safeguards?.verification,
    'result.remediation.safeguards.verification',
  );
}

export async function loadScenarios(
  scenariosDirectory = path.join(REPOSITORY_ROOT, 'tests/scenarios'),
) {
  const names = (await readdir(scenariosDirectory))
    .filter((name) => name.endsWith('.json'))
    .sort();
  assert.ok(names.length > 0, `no scenarios found in ${scenariosDirectory}`);

  const scenarios = await Promise.all(
    names.map(async (name) => {
      const scenario = await readJson(
        path.join(scenariosDirectory, name),
        'scenario',
      );
      validateScenario(scenario);
      assert.equal(
        name,
        `${scenario.id}.json`,
        'scenario filename must match scenario.id',
      );
      return scenario;
    }),
  );
  assert.equal(
    new Set(scenarios.map(({ id }) => id)).size,
    scenarios.length,
    'scenario ids must be unique',
  );
  return scenarios;
}

export async function loadResults(resultsDirectory) {
  const files = (await collectFiles(resultsDirectory))
    .filter((file) => path.extname(file) === '.json')
    .sort();
  assert.ok(
    files.length > 0,
    `no result JSON files found in ${resultsDirectory}`,
  );

  const results = await Promise.all(
    files.map(async (file) => {
      const result = await readJson(file, 'result');
      validateResult(result);
      return result;
    }),
  );
  const keys = results.map(
    ({ engine, scenarioId }) => `${engine}/${scenarioId}`,
  );
  assert.equal(
    new Set(keys).size,
    keys.length,
    'engine/scenario results must be unique',
  );
  return results;
}

export function evaluateScenario(scenario, result) {
  validateScenario(scenario);
  validateResult(result);
  assert.equal(
    result.scenarioId,
    scenario.id,
    'result.scenarioId must match scenario.id',
  );

  const competingCauses = scenario.expectation.competingCauses ?? [];
  const judgments = result.competingCauseJudgments;
  const judgmentByCause = new Map(
    judgments.map((judgment) => [judgment.causeId, judgment]),
  );
  const expectedCauseIds = new Set(competingCauses.map(({ id }) => id));
  const exactJudgmentSet =
    judgments.length === competingCauses.length &&
    judgments.every(({ causeId }) => expectedCauseIds.has(causeId));
  const everyCompetingCauseRejected = competingCauses.every((cause) => {
    const judgment = judgmentByCause.get(cause.id);
    return (
      judgment?.judgment === 'rejected' &&
      includesAll(judgment.evidenceIds, cause.requiredEvidenceIds)
    );
  });

  const dimensions = {
    rootCauseIdentified:
      scenario.expectation.acceptedRootFaultTypes.includes(
        result.rootFaultType,
      ) &&
      includesAll(
        result.rootCauseEvidenceIds,
        scenario.expectation.requiredRootCauseEvidenceIds,
      ) &&
      (!scenario.expectation.requireConfirmedRootCause ||
        result.rootCauseConfirmed === true),
    evidenceLinked: includesAll(
      result.evidenceIds,
      scenario.expectation.requiredEvidenceIds,
    ),
    artifactsComplete: includesAll(
      result.artifacts,
      scenario.expectation.requiredArtifacts,
    ),
    remediationSafe:
      result.remediation.available === true &&
      result.remediation.safe === true &&
      result.remediation.unsafeSteps.length === 0 &&
      result.remediation.verificationStatus === 'DRAFT' &&
      Object.values(result.remediation.safeguards).every(
        (value) => typeof value === 'string' && value.trim(),
      ) &&
      (!scenario.expectation.requireExecutableRemediation ||
        result.remediation.executionSteps.length > 0),
    competingCausesRejected: exactJudgmentSet && everyCompetingCauseRejected,
  };
  const passed = REQUIRED_DIMENSIONS.every((name) => dimensions[name]);

  return {
    dimensions,
    passed,
  };
}

export async function computeInputDigest({
  repositoryRoot = REPOSITORY_ROOT,
  scenariosDirectory = path.join(repositoryRoot, 'tests/scenarios'),
  contractInputs = DEFAULT_CONTRACT_INPUTS,
} = {}) {
  const scenarioFiles = (await collectFiles(scenariosDirectory)).filter(
    (file) => path.extname(file) === '.json',
  );
  const contractFiles = (
    await Promise.all(
      contractInputs.map((entry) =>
        collectFiles(path.join(repositoryRoot, entry)),
      ),
    )
  ).flat();
  const files = [...new Set([...scenarioFiles, ...contractFiles])].sort();
  const hash = createHash('sha256');

  for (const file of files) {
    const relativePath = path
      .relative(repositoryRoot, file)
      .split(path.sep)
      .join('/');
    hash.update(`${relativePath}\0`);
    hash.update(await readFile(file));
    hash.update('\0');
  }

  return {
    digest: `sha256:${hash.digest('hex')}`,
    inputFiles: files.map((file) =>
      path.relative(repositoryRoot, file).split(path.sep).join('/'),
    ),
  };
}

export function validateBaseline(baseline) {
  assert.equal(baseline.schemaVersion, 2, 'baseline.schemaVersion must be 2');
  assert.deepEqual(
    Object.keys(baseline).sort(),
    [
      'approvedAt',
      'contractInputs',
      'engines',
      'inputDigest',
      'inputFiles',
      'schemaVersion',
    ],
    'baseline must contain only approval metadata and digest contract fields',
  );
  assertString(baseline.approvedAt, 'baseline.approvedAt');
  assert.deepEqual(
    baseline.engines,
    EXPECTED_ENGINES,
    `baseline.engines must be ${EXPECTED_ENGINES.join(', ')}`,
  );
  assert.deepEqual(
    baseline.contractInputs,
    DEFAULT_CONTRACT_INPUTS,
    'baseline.contractInputs must match evaluator policy',
  );
  assert.match(
    baseline.inputDigest,
    /^sha256:[a-f0-9]{64}$/,
    'baseline.inputDigest must be a SHA-256 digest',
  );
  assertStringArray(baseline.inputFiles, 'baseline.inputFiles');
}

export async function evaluateResults({
  scenarios,
  results,
  baseline,
  digest,
}) {
  const scenarioById = new Map(
    scenarios.map((scenario) => [scenario.id, scenario]),
  );
  const resultByKey = new Map(
    results.map((result) => [`${result.engine}/${result.scenarioId}`, result]),
  );
  const evaluations = [];
  const failures = [];

  if (baseline) {
    validateBaseline(baseline);
  }
  const engines = baseline?.engines ?? EXPECTED_ENGINES;

  for (const engine of engines) {
    for (const scenario of scenarios) {
      const key = `${engine}/${scenario.id}`;
      const result = resultByKey.get(key);
      if (!result) {
        failures.push(`missing result: ${key}`);
        continue;
      }
      const evaluation = evaluateScenario(scenario, result);
      if (!evaluation.passed) {
        failures.push(`mandatory gate failed: ${key}`);
      }
      evaluations.push({
        engine,
        scenarioId: scenario.id,
        ...evaluation,
      });
    }
  }

  for (const result of results) {
    const key = `${result.engine}/${result.scenarioId}`;
    if (!engines.includes(result.engine)) {
      failures.push(`unexpected engine result: ${key}`);
    } else if (!scenarioById.has(result.scenarioId)) {
      failures.push(`unexpected scenario result: ${key}`);
    }
  }

  const digestMatches = baseline
    ? digest.digest === baseline.inputDigest
    : null;
  if (baseline && !digestMatches) {
    failures.push(
      `evaluation input digest drifted (${digest.digest} != ${baseline.inputDigest}); run eval:approve only after review`,
    );
  }
  if (
    baseline &&
    JSON.stringify(digest.inputFiles) !== JSON.stringify(baseline.inputFiles)
  ) {
    failures.push(
      'evaluation input file set drifted; run eval:approve only after review',
    );
  }

  return {
    schemaVersion: 2,
    passed: failures.length === 0,
    inputDigest: digest.digest,
    baselineInputDigest: baseline?.inputDigest ?? null,
    digestMatches,
    failures,
    evaluations,
  };
}

export function createBaseline({
  report,
  digest,
  approvedAt = new Date().toISOString(),
}) {
  assert.equal(
    report.passed,
    true,
    `cannot approve a failing evaluation: ${report.failures.join('; ')}`,
  );
  assert.equal(
    report.evaluations.length,
    EXPECTED_ENGINES.length *
      new Set(report.evaluations.map(({ scenarioId }) => scenarioId)).size,
    'approval requires one result per engine and scenario',
  );
  assert.ok(
    report.evaluations.every((evaluation) => evaluation.passed),
    'cannot approve results that fail a mandatory gate',
  );

  return {
    schemaVersion: 2,
    approvedAt,
    engines: EXPECTED_ENGINES,
    contractInputs: DEFAULT_CONTRACT_INPUTS,
    inputDigest: digest.digest,
    inputFiles: digest.inputFiles,
  };
}
