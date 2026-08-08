import { spawn } from 'node:child_process';
import { mkdir, mkdtemp, writeFile } from 'node:fs/promises';
import path from 'node:path';

import {
  computeInputDigest,
  evaluateResults,
  EXPECTED_ENGINES,
  loadScenarios,
  REPOSITORY_ROOT,
  validateResult,
} from './evaluator.mjs';
import { isMain, readJsonFile, writeJsonFile } from './cli-utils.mjs';

const COMMAND_ENV = {
  'cc-headless': 'RCA_EVAL_CC_HEADLESS_COMMAND',
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

export function validateModelEnvironment(env = process.env) {
  const commands = Object.fromEntries(
    EXPECTED_ENGINES.map((engine) => [
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
  if (env.CLAUDE_CODE_USE_BEDROCK !== '1') {
    throw new Error(
      'CLAUDE_CODE_USE_BEDROCK must be 1 so CC Headless model evaluation uses the deployed Bedrock backend.',
    );
  }
  if (!env.ANTHROPIC_DEFAULT_SONNET_MODEL) {
    throw new Error(
      'Missing ANTHROPIC_DEFAULT_SONNET_MODEL. Set it to the model ID deployed by the CC Headless task.',
    );
  }
  if (!env.RCA_EVAL_DEPLOYED_CC_MODEL) {
    throw new Error(
      'Missing RCA_EVAL_DEPLOYED_CC_MODEL. Set it to the model ID from the deployed CC Headless task definition.',
    );
  }
  if (env.ANTHROPIC_DEFAULT_SONNET_MODEL !== env.RCA_EVAL_DEPLOYED_CC_MODEL) {
    throw new Error(
      'Model contract mismatch: ANTHROPIC_DEFAULT_SONNET_MODEL must exactly match RCA_EVAL_DEPLOYED_CC_MODEL.',
    );
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
  timeoutMs = Number(env.RCA_EVAL_TIMEOUT_MS ?? DEFAULT_MODEL_TIMEOUT_MS),
} = {}) {
  const commands = validateModelEnvironment(env);
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
  for (const engine of EXPECTED_ENGINES) {
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

  const evaluation = await evaluateResults({
    scenarios: modelScenarios,
    results,
    baseline,
    digest,
  });
  const report = {
    ...evaluation,
    generatedAt: new Date().toISOString(),
    executionMode: 'model-eval',
    modelContract: {
      anthropicDefaultSonnetModel: env.ANTHROPIC_DEFAULT_SONNET_MODEL,
      deployedCcHeadlessModel: env.RCA_EVAL_DEPLOYED_CC_MODEL,
      matches:
        env.ANTHROPIC_DEFAULT_SONNET_MODEL === env.RCA_EVAL_DEPLOYED_CC_MODEL,
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

export async function main() {
  const { report, reportPath, resultsDirectory } = await runModelEvaluation();
  if (!report.passed) {
    throw new Error(
      `Model RCA evaluation failed; report: ${reportPath}\n- ${report.failures.join('\n- ')}`,
    );
  }
  process.stdout.write(
    `Model RCA evaluation passed. Results: ${resultsDirectory}\nReport: ${reportPath}\n`,
  );
}

if (isMain(import.meta.url)) {
  main().catch((error) => {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  });
}
