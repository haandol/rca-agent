import assert from 'node:assert/strict';
import { mkdtemp, readdir, readFile, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';

import { approveBaseline } from './approve-cli.mjs';
import { readJsonFile } from './cli-utils.mjs';
import {
  computeInputDigest,
  createBaseline,
  EXPECTED_ENGINES,
  loadScenarios,
  REPOSITORY_ROOT,
  validateBaseline,
} from './evaluator.mjs';
import {
  resolveRequestedEngines,
  runEngineCommand,
  runModelEvaluation,
  validateModelEnvironment,
} from './model-cli.mjs';

const fixturesDirectory = path.join(REPOSITORY_ROOT, 'tests/fixtures/results');
const fakeEnginePath = path.join(
  REPOSITORY_ROOT,
  'tests/fixtures/fake-engine.mjs',
);

test('model evaluation fails with actionable missing command errors', () => {
  assert.throws(
    () => validateModelEnvironment({ AWS_REGION: 'ap-northeast-2' }),
    /Missing RCA_EVAL_HEADLESS_CODEX_COMMAND/,
  );
});

test('model evaluation requires the deployed Codex model contract', () => {
  const commands = {
    AWS_PROFILE: 'fake-test-profile',
    AWS_REGION: 'us-east-1',
    RCA_EVAL_HEADLESS_CODEX_COMMAND: '["headless-codex"]',
    RCA_EVAL_STRANDS_COMMAND: '["strands"]',
  };

  assert.throws(
    () => validateModelEnvironment(commands),
    /CODEX_MODEL must be global\.openai\.gpt-5\.6-sol/,
  );
  assert.throws(
    () =>
      validateModelEnvironment({
        ...commands,
        CODEX_MODEL: 'global.openai.gpt-5.6-sol',
      }),
    /CODEX_REASONING_EFFORT must be high/,
  );
  assert.throws(
    () =>
      validateModelEnvironment({
        ...commands,
        CODEX_MODEL: 'global.openai.gpt-5.6-sol',
        CODEX_REASONING_EFFORT: 'high',
      }),
    /CODEX_MODEL_PROVIDER must be amazon-bedrock-runtime/,
  );
  assert.throws(
    () =>
      validateModelEnvironment({
        ...commands,
        CODEX_MODEL: 'global.openai.gpt-5.6-sol',
        CODEX_REASONING_EFFORT: 'high',
        CODEX_MODEL_PROVIDER: 'amazon-bedrock-runtime',
      }),
    /Missing RCA_EVAL_DEPLOYED_CODEX_MODEL/,
  );
  assert.throws(
    () =>
      validateModelEnvironment({
        ...commands,
        CODEX_MODEL: 'global.openai.gpt-5.6-sol',
        CODEX_REASONING_EFFORT: 'high',
        CODEX_MODEL_PROVIDER: 'amazon-bedrock-runtime',
        RCA_EVAL_DEPLOYED_CODEX_MODEL: 'different-deployed-model',
        RCA_EVAL_DEPLOYED_CODEX_REASONING_EFFORT: 'high',
        RCA_EVAL_DEPLOYED_CODEX_PROVIDER: 'amazon-bedrock-runtime',
      }),
    /Codex model contract mismatch/,
  );
});

test('model command receives its isolated failure evidence directory', async () => {
  const directory = await mkdtemp(path.join(tmpdir(), 'rca-model-failure-'));
  const failureDirectory = path.join(
    directory,
    'failures',
    'strands',
    'scenario-1',
  );
  const script = `
    process.stdout.write(JSON.stringify({
      schemaVersion: 2,
      scenarioId: "scenario-1",
      engine: "strands",
      rootCause: "A human-readable conclusion.",
      rootCauseConfirmed: true,
      rootFaultType: "db-leak",
      rootCauseEvidenceIds: ["evidence-1"],
      evidenceIds: ["evidence-1"],
      artifacts: ["scoping", "hypotheses", "validation", "report", "playbook"],
      competingCauseJudgments: [],
      remediation: {
        summary: "A human-readable response.",
        available: true,
        verificationStatus: "DRAFT",
        executionSteps: [{
          stepId: "restore-service",
          intent: "Restore service.",
          action: "Apply the approved reversible change.",
          successCriteria: "The alarm recovers."
        }],
        safe: true,
        unsafeSteps: [],
        safeguards: {
          preconditions: "confirmed",
          approval: "required",
          rollback: "previous revision",
          verification: "alarm OK"
        }
      },
      failureDirectory: process.env.RCA_EVAL_FAILURE_DIR
    }));
  `;

  const result = await runEngineCommand({
    command: [process.execPath, '-e', script],
    engine: 'strands',
    failureDirectory,
    scenario: { id: 'scenario-1' },
    scenarioPath: path.join(directory, 'scenario-1.json'),
    env: process.env,
    timeoutMs: 10_000,
  });

  assert.equal(result.failureDirectory, failureDirectory);
});

test('model command preserves parsed payloads that fail result validation', async () => {
  const directory = await mkdtemp(path.join(tmpdir(), 'rca-model-invalid-'));
  const failureDirectory = path.join(directory, 'failures');
  const validResult = {
    schemaVersion: 2,
    scenarioId: 'scenario-1',
    engine: 'strands',
    rootCause: 'A human-readable conclusion.',
    rootCauseConfirmed: true,
    rootFaultType: 'db-leak',
    rootCauseEvidenceIds: ['evidence-1'],
    evidenceIds: ['evidence-1'],
    artifacts: ['report'],
    competingCauseJudgments: [],
    remediation: {
      summary: 'A human-readable response.',
      available: true,
      verificationStatus: 'DRAFT',
      executionSteps: [
        {
          stepId: 'restore-service',
          intent: 'Restore service.',
          action: 'Apply the approved reversible change.',
          successCriteria: 'The alarm recovers.',
        },
      ],
      safe: true,
      unsafeSteps: [],
      safeguards: {
        preconditions: 'confirmed',
        approval: 'required',
        rollback: 'previous revision',
        verification: 'alarm OK',
      },
    },
  };
  const { rootCause: _rootCause, ...schemaInvalid } = validResult;
  const wrongIdentity = {
    ...validResult,
    scenarioId: 'different-scenario',
  };

  for (const payload of [schemaInvalid, wrongIdentity]) {
    const script = `process.stdout.write(${JSON.stringify(
      JSON.stringify(payload),
    )});`;
    await assert.rejects(
      runEngineCommand({
        command: [process.execPath, '-e', script],
        engine: 'strands',
        failureDirectory,
        scenario: { id: 'scenario-1' },
        scenarioPath: path.join(directory, 'scenario-1.json'),
        env: process.env,
        timeoutMs: 10_000,
      }),
      /returned an invalid normalized result/,
    );
  }

  const entries = await readdir(failureDirectory, { withFileTypes: true });
  assert.equal(entries.length, 2);
  assert.ok(entries.every((entry) => entry.isDirectory()));
  assert.equal(new Set(entries.map(({ name }) => name)).size, 2);
  const diagnostics = await Promise.all(
    entries.map(({ name }) =>
      readJsonFile(
        path.join(failureDirectory, name, 'diagnostic.json'),
        'model validation diagnostic',
      ),
    ),
  );
  const schemaDiagnostic = diagnostics.find(
    ({ normalizedResult }) => !normalizedResult.rootCause,
  );
  const identityDiagnostic = diagnostics.find(
    ({ normalizedResult }) =>
      normalizedResult.scenarioId === 'different-scenario',
  );

  assert.equal(schemaDiagnostic.schemaVersion, 1);
  assert.deepEqual(schemaDiagnostic.normalizedResult, schemaInvalid);
  assert.match(schemaDiagnostic.validationError, /rootCause must be a string/);
  assert.deepEqual(identityDiagnostic.normalizedResult, wrongIdentity);
  assert.match(identityDiagnostic.validationError, /result identity must be/);
  assert.ok(
    diagnostics.every(
      (diagnostic) =>
        !Object.hasOwn(diagnostic, 'stdout') &&
        !Object.hasOwn(diagnostic, 'rawStdout'),
    ),
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
  assert.equal(baseline.schemaVersion, 2);
  assert.deepEqual(Object.keys(baseline).sort(), [
    'approvedAt',
    'contractInputs',
    'engines',
    'inputDigest',
    'inputFiles',
    'schemaVersion',
  ]);
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
    CODEX_MODEL: 'global.openai.gpt-5.6-sol',
    CODEX_REASONING_EFFORT: 'high',
    CODEX_MODEL_PROVIDER: 'amazon-bedrock-runtime',
    RCA_EVAL_DEPLOYED_CODEX_MODEL: 'global.openai.gpt-5.6-sol',
    RCA_EVAL_DEPLOYED_CODEX_REASONING_EFFORT: 'high',
    RCA_EVAL_DEPLOYED_CODEX_PROVIDER: 'amazon-bedrock-runtime',
    RCA_EVAL_HEADLESS_CODEX_COMMAND: JSON.stringify([
      ...baseCommand,
      'headless-codex',
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
  assert.equal(outcome.report.schemaVersion, 2);
  assert.equal(outcome.report.executionMode, 'model-eval');
  assert.deepEqual(outcome.report.modelContract, {
    codexModel: 'global.openai.gpt-5.6-sol',
    codexReasoningEffort: 'high',
    codexProvider: 'amazon-bedrock-runtime',
    deployedCodexModel: 'global.openai.gpt-5.6-sol',
    deployedCodexReasoningEffort: 'high',
    deployedCodexProvider: 'amazon-bedrock-runtime',
    matches: true,
  });
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
  assert.deepEqual(outcome.report.enginesRun, EXPECTED_ENGINES);
  assert.deepEqual(outcome.report.enginesReused, []);
  assert.equal(outcome.report.enginesComplete, true);
});

test('an engine selection is validated against the declared engines', () => {
  assert.deepEqual(resolveRequestedEngines(undefined), EXPECTED_ENGINES);
  assert.deepEqual(resolveRequestedEngines([]), EXPECTED_ENGINES);
  assert.deepEqual(resolveRequestedEngines(['strands']), ['strands']);
  // Declared order wins over flag order so a resumed run reports like a full one.
  assert.deepEqual(
    resolveRequestedEngines(['strands', 'headless-codex']),
    EXPECTED_ENGINES,
  );
  assert.throws(
    () => resolveRequestedEngines(['stands']),
    /Unknown engine\(s\): stands/,
  );
});

test('one engine can be evaluated alone and the report says so', async () => {
  const directory = await mkdtemp(path.join(tmpdir(), 'rca-model-one-'));
  const baselinePath = path.join(directory, 'baseline.json');
  const resultsDirectory = path.join(directory, 'model-results');
  await approveBaseline({
    resultsDirectory: fixturesDirectory,
    baselinePath,
    approvedAt: '2026-07-21T00:00:00.000Z',
  });

  const outcome = await runModelEvaluation({
    // Only the engine under evaluation needs a command configured; requiring the
    // other one would defeat the point of narrowing the run.
    env: {
      ...process.env,
      AWS_PROFILE: 'fake-test-profile',
      AWS_REGION: 'ap-northeast-2',
      RCA_EVAL_STRANDS_COMMAND: JSON.stringify([
        process.execPath,
        fakeEnginePath,
        'strands',
      ]),
      RCA_EVAL_HEADLESS_CODEX_COMMAND: undefined,
      CODEX_MODEL: undefined,
      CODEX_REASONING_EFFORT: undefined,
      CODEX_MODEL_PROVIDER: undefined,
      RCA_EVAL_DEPLOYED_CODEX_MODEL: undefined,
      RCA_EVAL_DEPLOYED_CODEX_REASONING_EFFORT: undefined,
      RCA_EVAL_DEPLOYED_CODEX_PROVIDER: undefined,
    },
    baselinePath,
    resultsDirectory,
    reportPath: path.join(directory, 'report.json'),
    engines: ['strands'],
    timeoutMs: 10_000,
  });

  assert.equal(outcome.report.passed, true, outcome.report.failures.join('\n'));
  assert.deepEqual(outcome.report.engines, ['strands']);
  assert.equal(outcome.report.enginesComplete, false);
  // The skipped engine must not be scored as a missing result — the round did
  // not claim to cover it.
  assert.deepEqual(outcome.report.failures, []);
  assert.ok(
    outcome.report.evaluations.every(({ engine }) => engine === 'strands'),
  );
  await assert.rejects(
    async () => stat(path.join(resultsDirectory, 'headless-codex')),
    /ENOENT/,
  );
});

test('a partial round cannot be approved until the other engine runs into it', async () => {
  const directory = await mkdtemp(path.join(tmpdir(), 'rca-model-partial-'));
  const baselinePath = path.join(directory, 'baseline.json');
  const resultsDirectory = path.join(directory, 'model-results');
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
    CODEX_MODEL: 'global.openai.gpt-5.6-sol',
    CODEX_REASONING_EFFORT: 'high',
    CODEX_MODEL_PROVIDER: 'amazon-bedrock-runtime',
    RCA_EVAL_DEPLOYED_CODEX_MODEL: 'global.openai.gpt-5.6-sol',
    RCA_EVAL_DEPLOYED_CODEX_REASONING_EFFORT: 'high',
    RCA_EVAL_DEPLOYED_CODEX_PROVIDER: 'amazon-bedrock-runtime',
    RCA_EVAL_HEADLESS_CODEX_COMMAND: JSON.stringify([
      ...baseCommand,
      'headless-codex',
      '{scenario}',
    ]),
    RCA_EVAL_STRANDS_COMMAND: JSON.stringify([...baseCommand, 'strands']),
  };
  const common = {
    env,
    baselinePath,
    resultsDirectory,
    reportPath: path.join(directory, 'report.json'),
    timeoutMs: 10_000,
  };

  const first = await runModelEvaluation({ ...common, engines: ['strands'] });
  assert.equal(first.report.enginesComplete, false);
  const digest = await computeInputDigest();
  assert.throws(
    () => createBaseline({ report: first.report, digest }),
    /approval requires every engine/,
    'one engine must not be able to define the baseline both engines are compared against',
  );

  // The second run covers the remaining engine and reuses what is already on
  // disk, so the round reaches full coverage without repeating the first engine.
  const second = await runModelEvaluation({
    ...common,
    engines: ['headless-codex'],
  });
  assert.equal(second.report.passed, true, second.report.failures.join('\n'));
  assert.deepEqual(second.report.enginesRun, ['headless-codex']);
  assert.deepEqual(second.report.enginesReused, ['strands']);
  assert.equal(second.report.enginesComplete, true);
  assert.deepEqual(second.report.engines, EXPECTED_ENGINES);
  assert.doesNotThrow(() => createBaseline({ report: second.report, digest }));
});
