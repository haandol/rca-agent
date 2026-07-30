import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readdir, readFile, stat } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const SLUG_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const CONTRACT_EXTENSIONS = new Set(['.json', '.md', '.py']);

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
  // Each engine's live adapter decides how a run becomes a normalized result, so
  // changing one changes what the scores mean and requires re-approval.
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
  // remediationSafe scores the proposed procedure against the destructive-action
  // vocabulary, so a change to that vocabulary changes what the dimension means.
  'packages/agent/src/rca_agent/services/destructive_actions.py',
  'packages/cc-headless/src/cc_headless/services/destructive_actions.py',
];

function normalize(value) {
  return String(value ?? '')
    .normalize('NFKC')
    .toLowerCase();
}

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

function assertTermGroups(groups, label, minimum = 1) {
  assert.ok(Array.isArray(groups), `${label} must be an array`);
  assert.ok(
    groups.length >= minimum,
    `${label} must contain ${minimum} group(s)`,
  );
  groups.forEach((group, index) =>
    assertStringArray(group, `${label}[${index}]`),
  );
}

function termGroupCoverage(text, groups) {
  const normalized = normalize(text);
  const matched = groups.filter((group) =>
    group.some((term) => normalized.includes(normalize(term))),
  ).length;
  return matched / groups.length;
}

function includesAll(actual, expected) {
  const values = new Set(actual);
  return expected.every((value) => values.has(value));
}

function coverage(actual, expected) {
  const values = new Set(actual);
  return expected.filter((value) => values.has(value)).length / expected.length;
}

function roundScore(value) {
  return Math.round(value * 1_000_000) / 1_000_000;
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
  assertTermGroups(
    expectation.rootCauseTermGroups,
    'scenario.expectation.rootCauseTermGroups',
    2,
  );
  assertStringArray(
    expectation.requiredEvidenceIds,
    'scenario.expectation.requiredEvidenceIds',
  );
  const observationIds = new Set(
    scenario.observations.map((observation) => observation.id),
  );
  assert.ok(
    expectation.requiredEvidenceIds.every((id) => observationIds.has(id)),
    'required evidence ids must reference scenario observations',
  );
  assertStringArray(
    expectation.requiredArtifacts,
    'scenario.expectation.requiredArtifacts',
  );
  assert.ok(
    expectation.requiredArtifacts.includes('report'),
    'required artifacts must include report',
  );
  assertTermGroups(
    expectation.remediationTermGroups,
    'scenario.expectation.remediationTermGroups',
    2,
  );
  assertTermGroups(
    expectation.semanticTermGroups,
    'scenario.expectation.semanticTermGroups',
  );
  if (Object.hasOwn(expectation, 'rejectedCauseTermGroups')) {
    assertTermGroups(
      expectation.rejectedCauseTermGroups,
      'scenario.expectation.rejectedCauseTermGroups',
    );
  }
  assert.equal(
    Object.hasOwn(scenario, 'engineSamples'),
    false,
    'scenario fixtures must not contain engineSamples',
  );
}

export function validateResult(result) {
  assert.equal(typeof result, 'object', 'result must be an object');
  assert.ok(result, 'result must not be null');
  assert.equal(result.schemaVersion, 1, 'result.schemaVersion must be 1');
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
  assertStringArray(result.evidenceIds, 'result.evidenceIds');
  assertStringArray(result.artifacts, 'result.artifacts');
  assertString(result.remediation?.summary, 'result.remediation.summary');
  assert.equal(
    typeof result.remediation?.safe,
    'boolean',
    'result.remediation.safe must be a boolean',
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

  const rootCauseCoverage = termGroupCoverage(
    result.rootCause,
    scenario.expectation.rootCauseTermGroups,
  );
  const evidenceCoverage = coverage(
    result.evidenceIds,
    scenario.expectation.requiredEvidenceIds,
  );
  const artifactCoverage = coverage(
    result.artifacts,
    scenario.expectation.requiredArtifacts,
  );
  const remediationCoverage = termGroupCoverage(
    result.remediation.summary,
    scenario.expectation.remediationTermGroups,
  );
  const semanticSpecificity = termGroupCoverage(
    `${result.rootCause}\n${result.remediation.summary}`,
    scenario.expectation.semanticTermGroups,
  );
  // Naming a competing cause as the root cause is a precision failure, not just
  // a lower score. Absence of every rejected group is what makes the dimension
  // pass, so a scenario without the field is trivially satisfied.
  const rejectedCauseTermGroups =
    scenario.expectation.rejectedCauseTermGroups ?? [];
  const rejectedCauseCoverage = rejectedCauseTermGroups.length
    ? termGroupCoverage(result.rootCause, rejectedCauseTermGroups)
    : 0;

  const dimensions = {
    rootCauseIdentified: rootCauseCoverage === 1,
    evidenceLinked: includesAll(
      result.evidenceIds,
      scenario.expectation.requiredEvidenceIds,
    ),
    artifactsComplete: includesAll(
      result.artifacts,
      scenario.expectation.requiredArtifacts,
    ),
    remediationSafe:
      result.remediation.safe === true &&
      remediationCoverage === 1 &&
      Object.values(result.remediation.safeguards).every(
        (value) => typeof value === 'string' && value.trim(),
      ),
    competingCausesRejected: rejectedCauseCoverage === 0,
  };
  const passed = REQUIRED_DIMENSIONS.every((name) => dimensions[name]);
  const semanticScore = roundScore(
    rootCauseCoverage * 0.3 +
      evidenceCoverage * 0.2 +
      artifactCoverage * 0.15 +
      remediationCoverage * 0.2 +
      semanticSpecificity * 0.15,
  );

  return {
    dimensions,
    passed,
    semanticScore,
    semanticComponents: {
      rootCauseCoverage: roundScore(rootCauseCoverage),
      evidenceCoverage: roundScore(evidenceCoverage),
      artifactCoverage: roundScore(artifactCoverage),
      remediationCoverage: roundScore(remediationCoverage),
      semanticSpecificity: roundScore(semanticSpecificity),
      rejectedCauseCoverage: roundScore(rejectedCauseCoverage),
    },
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
  assert.equal(baseline.schemaVersion, 1, 'baseline.schemaVersion must be 1');
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
  assert.equal(
    typeof baseline.semanticScores,
    'object',
    'baseline.semanticScores is required',
  );
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
      const baselineSemanticScore =
        baseline?.semanticScores?.[engine]?.[scenario.id];
      if (baseline && typeof baselineSemanticScore !== 'number') {
        failures.push(`missing baseline semantic score: ${key}`);
      }
      const semanticRegression =
        typeof baselineSemanticScore === 'number' &&
        evaluation.semanticScore < baselineSemanticScore;
      if (!evaluation.passed) {
        failures.push(`mandatory gate failed: ${key}`);
      }
      if (semanticRegression) {
        failures.push(
          `semantic regression: ${key} (${evaluation.semanticScore} < ${baselineSemanticScore})`,
        );
      }
      evaluations.push({
        engine,
        scenarioId: scenario.id,
        ...evaluation,
        baselineSemanticScore: baselineSemanticScore ?? null,
        semanticRegression,
        passed: evaluation.passed && !semanticRegression,
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
    schemaVersion: 1,
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

  const semanticScores = Object.fromEntries(
    EXPECTED_ENGINES.map((engine) => [
      engine,
      Object.fromEntries(
        report.evaluations
          .filter((evaluation) => evaluation.engine === engine)
          .map((evaluation) => [
            evaluation.scenarioId,
            evaluation.semanticScore,
          ])
          .sort(([left], [right]) => left.localeCompare(right)),
      ),
    ]),
  );

  return {
    schemaVersion: 1,
    approvedAt,
    engines: EXPECTED_ENGINES,
    contractInputs: DEFAULT_CONTRACT_INPUTS,
    inputDigest: digest.digest,
    inputFiles: digest.inputFiles,
    semanticScores,
  };
}
