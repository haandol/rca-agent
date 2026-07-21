import { spawn } from 'node:child_process';
import { mkdir } from 'node:fs/promises';
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

export function validateLiveEnvironment(env = process.env) {
  const commands = Object.fromEntries(
    EXPECTED_ENGINES.map((engine) => [
      engine,
      parseCommand(env[COMMAND_ENV[engine]], COMMAND_ENV[engine]),
    ]),
  );
  if (!(env.AWS_REGION || env.AWS_DEFAULT_REGION)) {
    throw new Error(
      'Missing AWS_REGION (or AWS_DEFAULT_REGION). Set the AWS region used by both live engines.',
    );
  }
  if (!hasAwsCredentials(env)) {
    throw new Error(
      'Missing AWS credentials. Set AWS_PROFILE, an access-key pair, a container credential URI, or web-identity variables.',
    );
  }
  return commands;
}

export function runEngineCommand({
  command,
  engine,
  scenario,
  scenarioPath,
  env = process.env,
  timeoutMs = 15 * 60 * 1000,
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
      env,
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
    child.on('close', (code, signal) => {
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
        reject(
          new Error(
            `${engine} returned an invalid normalized result: ${error.message}`,
          ),
        );
      }
    });

    child.stdin.end(usesScenarioPath ? undefined : JSON.stringify(scenario));
  });
}

export async function runLiveEvaluation({
  env = process.env,
  repositoryRoot = REPOSITORY_ROOT,
  scenariosDirectory = path.join(repositoryRoot, 'tests/scenarios'),
  baselinePath,
  resultsDirectory,
  reportPath,
  timeoutMs = Number(env.RCA_EVAL_TIMEOUT_MS ?? 15 * 60 * 1000),
} = {}) {
  const commands = validateLiveEnvironment(env);
  const runId = new Date().toISOString().replaceAll(/[:.]/g, '-');
  const actualResultsDirectory =
    resultsDirectory ??
    (env.RCA_EVAL_RESULTS_DIR
      ? path.resolve(repositoryRoot, env.RCA_EVAL_RESULTS_DIR)
      : undefined) ??
    path.join(repositoryRoot, 'tests/results/live', runId, 'results');
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
  await mkdir(actualResultsDirectory, { recursive: true });

  const results = [];
  for (const engine of EXPECTED_ENGINES) {
    for (const scenario of scenarios) {
      const scenarioPath = path.join(scenariosDirectory, `${scenario.id}.json`);
      const result = await runEngineCommand({
        command: commands[engine],
        engine,
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
    scenarios,
    results,
    baseline,
    digest,
  });
  const report = {
    ...evaluation,
    generatedAt: new Date().toISOString(),
    live: true,
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
  const { report, reportPath, resultsDirectory } = await runLiveEvaluation();
  if (!report.passed) {
    throw new Error(
      `Live RCA evaluation failed; report: ${reportPath}\n- ${report.failures.join('\n- ')}`,
    );
  }
  process.stdout.write(
    `Live RCA evaluation passed. Results: ${resultsDirectory}\nReport: ${reportPath}\n`,
  );
}

if (isMain(import.meta.url)) {
  main().catch((error) => {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  });
}
