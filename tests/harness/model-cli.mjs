import { spawn } from 'node:child_process';
import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';

import {
  computeInputDigest,
  evaluateResults,
  EXPECTED_ENGINES,
  loadScenarios,
  REPOSITORY_ROOT,
  validateResult,
} from './evaluator.mjs';
import {
  isMain,
  parseCliOptions,
  readJsonFile,
  resolveFrom,
  writeJsonFile,
} from './cli-utils.mjs';

const COMMAND_ENV = {
  'headless-codex': 'RCA_EVAL_HEADLESS_CODEX_COMMAND',
  strands: 'RCA_EVAL_STRANDS_COMMAND',
};
const DEFAULT_MODEL_TIMEOUT_MS = 60 * 60 * 1000;

async function preserveResultValidationDiagnostics({
  failureDirectory,
  result,
  validationError,
}) {
  if (!failureDirectory) {
    return;
  }

  try {
    await mkdir(failureDirectory, { recursive: true, mode: 0o700 });
    const destination = await mkdtemp(
      path.join(failureDirectory, 'result-validation-'),
    );
    await writeFile(
      path.join(destination, 'diagnostic.json'),
      `${JSON.stringify(
        {
          schemaVersion: 1,
          normalizedResult: result,
          validationError: validationError.message,
        },
        null,
        2,
      )}\n`,
      { encoding: 'utf8', flag: 'wx', mode: 0o600 },
    );
    process.stderr.write(
      `eval result validation diagnostics preserved at ${destination}\n`,
    );
  } catch (error) {
    process.stderr.write(
      `failed to preserve eval result validation diagnostics: ${error.message}\n`,
    );
  }
}

export function resolveRequestedEngines(requested) {
  if (!requested || requested.length === 0) {
    return EXPECTED_ENGINES;
  }
  const unknown = requested.filter(
    (engine) => !EXPECTED_ENGINES.includes(engine),
  );
  if (unknown.length > 0) {
    throw new Error(
      `Unknown engine(s): ${unknown.join(', ')}. Expected one of ${EXPECTED_ENGINES.join(', ')}.`,
    );
  }
  // Keep the declared order regardless of flag order so a resumed run writes
  // and reports engines the same way a full run does.
  return EXPECTED_ENGINES.filter((engine) => requested.includes(engine));
}

async function readExistingResult(resultPath) {
  try {
    return JSON.parse(await readFile(resultPath, 'utf8'));
  } catch {
    return null;
  }
}

function parseCommand(value, variableName) {
  if (!value) {
    throw new Error(
      `Missing ${variableName}. Set it to a JSON argv array, for example '["python","engine.py"]'.`,
    );
  }
  let command;
  try {
    command = JSON.parse(value);
  } catch (error) {
    throw new Error(`${variableName} must be valid JSON: ${error.message}`, {
      cause: error,
    });
  }
  if (
    !Array.isArray(command) ||
    command.length === 0 ||
    command.some((entry) => typeof entry !== 'string' || !entry)
  ) {
    throw new Error(
      `${variableName} must be a non-empty JSON array of strings.`,
    );
  }
  return command;
}

function hasAwsCredentials(env) {
  return Boolean(
    env.AWS_PROFILE ||
    (env.AWS_ACCESS_KEY_ID && env.AWS_SECRET_ACCESS_KEY) ||
    env.AWS_CONTAINER_CREDENTIALS_RELATIVE_URI ||
    env.AWS_CONTAINER_CREDENTIALS_FULL_URI ||
    (env.AWS_WEB_IDENTITY_TOKEN_FILE && env.AWS_ROLE_ARN),
  );
}

export function validateModelEnvironment(
  env = process.env,
  engines = EXPECTED_ENGINES,
) {
  // Only the engines this run drives need a command: requiring the other one
  // would defeat the point of narrowing the run.
  const commands = Object.fromEntries(
    engines.map((engine) => [
      engine,
      parseCommand(env[COMMAND_ENV[engine]], COMMAND_ENV[engine]),
    ]),
  );
  if (!(env.AWS_REGION || env.AWS_DEFAULT_REGION)) {
    throw new Error(
      'Missing AWS_REGION (or AWS_DEFAULT_REGION). Set the AWS region used by both model-eval engines.',
    );
  }
  if (!hasAwsCredentials(env)) {
    throw new Error(
      'Missing AWS credentials. Set AWS_PROFILE, an access-key pair, a container credential URI, or web-identity variables.',
    );
  }
  // The deployed-model parity contract belongs to Headless Codex, so it is enforced
  // exactly when that engine is in scope.
  if (engines.includes('headless-codex')) {
    if (env.CODEX_MODEL !== 'global.openai.gpt-5.6-sol') {
      throw new Error(
        'CODEX_MODEL must be global.openai.gpt-5.6-sol for Headless Codex model evaluation.',
      );
    }
    if (env.CODEX_REASONING_EFFORT !== 'high') {
      throw new Error(
        'CODEX_REASONING_EFFORT must be high for Headless Codex model evaluation.',
      );
    }
    if (env.CODEX_MODEL_PROVIDER !== 'amazon-bedrock-runtime') {
      throw new Error(
        'CODEX_MODEL_PROVIDER must be amazon-bedrock-runtime for Headless Codex model evaluation.',
      );
    }
    for (const variable of [
      'RCA_EVAL_DEPLOYED_CODEX_MODEL',
      'RCA_EVAL_DEPLOYED_CODEX_REASONING_EFFORT',
      'RCA_EVAL_DEPLOYED_CODEX_PROVIDER',
    ]) {
      if (!env[variable]) {
        throw new Error(
          `Missing ${variable}. Set it from the deployed Headless Codex task definition.`,
        );
      }
    }
    if (
      env.CODEX_MODEL !== env.RCA_EVAL_DEPLOYED_CODEX_MODEL ||
      env.CODEX_REASONING_EFFORT !==
        env.RCA_EVAL_DEPLOYED_CODEX_REASONING_EFFORT ||
      env.CODEX_MODEL_PROVIDER !== env.RCA_EVAL_DEPLOYED_CODEX_PROVIDER
    ) {
      throw new Error(
        'Codex model contract mismatch: local model, reasoning effort, and provider must match the deployed task.',
      );
    }
  }
  return commands;
}

export function runEngineCommand({
  command,
  engine,
  failureDirectory,
  scenario,
  scenarioPath,
  env = process.env,
  timeoutMs = DEFAULT_MODEL_TIMEOUT_MS,
}) {
  const usesScenarioPath = command.some((argument) =>
    argument.includes('{scenario}'),
  );
  const argv = command.map((argument) =>
    argument
      .replaceAll('{scenario}', scenarioPath)
      .replaceAll('{scenarioId}', scenario.id),
  );

  return new Promise((resolve, reject) => {
    const child = spawn(argv[0], argv.slice(1), {
      cwd: REPOSITORY_ROOT,
      env: {
        ...env,
        ...(failureDirectory ? { RCA_EVAL_FAILURE_DIR: failureDirectory } : {}),
      },
      shell: false,
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      child.kill('SIGTERM');
    }, timeoutMs);

    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', (chunk) => {
      stdout += chunk;
    });
    child.stderr.on('data', (chunk) => {
      stderr += chunk;
    });
    child.on('error', (error) => {
      clearTimeout(timer);
      reject(
        new Error(
          `Unable to start ${engine} command '${argv[0]}': ${error.message}`,
          { cause: error },
        ),
      );
    });
    child.on('close', async (code, signal) => {
      clearTimeout(timer);
      if (timedOut) {
        reject(new Error(`${engine} command timed out after ${timeoutMs}ms`));
        return;
      }
      if (code !== 0) {
        reject(
          new Error(
            `${engine} command failed with code ${code ?? `signal ${signal}`}: ${stderr.trim()}`,
          ),
        );
        return;
      }
      let result;
      try {
        result = JSON.parse(stdout);
      } catch (error) {
        reject(
          new Error(
            `${engine} command must write exactly one normalized result JSON object to stdout: ${error.message}`,
            { cause: error },
          ),
        );
        return;
      }
      try {
        validateResult(result);
        if (result.engine !== engine || result.scenarioId !== scenario.id) {
          throw new Error(
            `result identity must be ${engine}/${scenario.id}, got ${result.engine}/${result.scenarioId}`,
          );
        }
        resolve(result);
      } catch (error) {
        await preserveResultValidationDiagnostics({
          failureDirectory,
          result,
          validationError: error,
        });
        reject(
          new Error(
            `${engine} returned an invalid normalized result: ${error.message}`,
            { cause: error },
          ),
        );
      }
    });

    child.stdin.end(usesScenarioPath ? undefined : JSON.stringify(scenario));
  });
}

export async function runModelEvaluation({
  env = process.env,
  repositoryRoot = REPOSITORY_ROOT,
  scenariosDirectory = path.join(repositoryRoot, 'tests/scenarios'),
  baselinePath,
  resultsDirectory,
  reportPath,
  engines,
  timeoutMs = Number(env.RCA_EVAL_TIMEOUT_MS ?? DEFAULT_MODEL_TIMEOUT_MS),
} = {}) {
  const requestedEngines = resolveRequestedEngines(engines);
  const commands = validateModelEnvironment(env, requestedEngines);
  const runId = new Date().toISOString().replaceAll(/[:.]/g, '-');
  const actualResultsDirectory =
    resultsDirectory ??
    (env.RCA_EVAL_RESULTS_DIR
      ? path.resolve(repositoryRoot, env.RCA_EVAL_RESULTS_DIR)
      : undefined) ??
    path.join(repositoryRoot, 'tests/results/model', runId, 'results');
  const actualReportPath =
    reportPath ??
    (env.RCA_EVAL_REPORT
      ? path.resolve(repositoryRoot, env.RCA_EVAL_REPORT)
      : path.join(path.dirname(actualResultsDirectory), 'report.json'));
  const actualBaselinePath =
    baselinePath ??
    path.resolve(
      repositoryRoot,
      env.RCA_EVAL_BASELINE ?? 'tests/baseline/rca-evaluation.json',
    );
  const [scenarios, baseline, digest] = await Promise.all([
    loadScenarios(scenariosDirectory),
    readJsonFile(actualBaselinePath, 'baseline'),
    computeInputDigest({ repositoryRoot, scenariosDirectory }),
  ]);
  const modelScenarios = scenarios.filter(({ executionModes }) =>
    executionModes.includes('model-eval'),
  );
  if (modelScenarios.length === 0) {
    throw new Error('No scenarios declare the model-eval execution mode.');
  }
  await mkdir(actualResultsDirectory, { recursive: true });

  const results = [];
  for (const engine of requestedEngines) {
    for (const scenario of modelScenarios) {
      const scenarioPath = path.join(scenariosDirectory, `${scenario.id}.json`);
      const result = await runEngineCommand({
        command: commands[engine],
        engine,
        failureDirectory: path.join(
          path.dirname(actualResultsDirectory),
          'failures',
          engine,
          scenario.id,
        ),
        scenario,
        scenarioPath,
        env,
        timeoutMs,
      });
      results.push(result);
      await writeJsonFile(
        path.join(actualResultsDirectory, engine, `${scenario.id}.json`),
        result,
      );
    }
  }

  // An engine this run skipped may already have results in the same directory
  // from an earlier partial run. Load them so the report reflects everything the
  // round has accumulated -- that is what makes a resumed run reach approval.
  const reusedEngines = [];
  for (const engine of EXPECTED_ENGINES) {
    if (requestedEngines.includes(engine)) {
      continue;
    }
    const reused = [];
    for (const scenario of modelScenarios) {
      const existing = await readExistingResult(
        path.join(actualResultsDirectory, engine, `${scenario.id}.json`),
      );
      if (existing) {
        reused.push(existing);
      }
    }
    if (reused.length > 0) {
      reusedEngines.push(engine);
      results.push(...reused);
    }
  }

  const coveredEngines = EXPECTED_ENGINES.filter(
    (engine) =>
      requestedEngines.includes(engine) || reusedEngines.includes(engine),
  );
  const evaluation = await evaluateResults({
    scenarios: modelScenarios,
    results,
    baseline,
    digest,
    engines: coveredEngines,
  });
  const report = {
    ...evaluation,
    generatedAt: new Date().toISOString(),
    executionMode: 'model-eval',
    enginesRun: requestedEngines,
    enginesReused: reusedEngines,
    modelContract: {
      codexModel: env.CODEX_MODEL,
      codexReasoningEffort: env.CODEX_REASONING_EFFORT,
      codexProvider: env.CODEX_MODEL_PROVIDER,
      deployedCodexModel: env.RCA_EVAL_DEPLOYED_CODEX_MODEL,
      deployedCodexReasoningEffort:
        env.RCA_EVAL_DEPLOYED_CODEX_REASONING_EFFORT,
      deployedCodexProvider: env.RCA_EVAL_DEPLOYED_CODEX_PROVIDER,
      matches:
        env.CODEX_MODEL === env.RCA_EVAL_DEPLOYED_CODEX_MODEL &&
        env.CODEX_REASONING_EFFORT ===
          env.RCA_EVAL_DEPLOYED_CODEX_REASONING_EFFORT &&
        env.CODEX_MODEL_PROVIDER === env.RCA_EVAL_DEPLOYED_CODEX_PROVIDER,
    },
    resultsDirectory: actualResultsDirectory,
  };
  await writeJsonFile(actualReportPath, report);
  return {
    report,
    reportPath: actualReportPath,
    resultsDirectory: actualResultsDirectory,
  };
}

export async function main(args = process.argv.slice(2)) {
  const options = parseCliOptions(args, { allowEngine: true });
  const { report, reportPath, resultsDirectory } = await runModelEvaluation({
    engines: options.engine,
    resultsDirectory: options.results
      ? resolveFrom(REPOSITORY_ROOT, options.results)
      : undefined,
    baselinePath: options.baseline
      ? resolveFrom(REPOSITORY_ROOT, options.baseline)
      : undefined,
    reportPath: options.report
      ? resolveFrom(REPOSITORY_ROOT, options.report)
      : undefined,
  });
  if (!report.passed) {
    throw new Error(
      `Model RCA evaluation failed; report: ${reportPath}\n- ${report.failures.join('\n- ')}`,
    );
  }
  process.stdout.write(
    `Model RCA evaluation passed for ${report.engines.join(', ')}. Results: ${resultsDirectory}\nReport: ${reportPath}\n`,
  );
  if (!report.enginesComplete) {
    const missing = EXPECTED_ENGINES.filter(
      (engine) => !report.engines.includes(engine),
    );
    process.stdout.write(
      `Partial round: ${missing.join(', ')} not covered. Approval needs every engine — rerun with --engine ${missing.join(' --engine ')} --results ${resultsDirectory}\n`,
    );
  }
}

if (isMain(import.meta.url)) {
  main().catch((error) => {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  });
}
