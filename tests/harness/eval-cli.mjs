import path from 'node:path';

import {
  computeInputDigest,
  evaluateResults,
  loadResults,
  loadScenarios,
  REPOSITORY_ROOT,
} from './evaluator.mjs';
import {
  isMain,
  parseCliOptions,
  readJsonFile,
  resolveFrom,
  writeJsonFile,
} from './cli-utils.mjs';

export async function runEvaluation({
  repositoryRoot = REPOSITORY_ROOT,
  scenariosDirectory = path.join(repositoryRoot, 'tests/scenarios'),
  resultsDirectory,
  baselinePath = path.join(
    repositoryRoot,
    'tests/baseline/rca-evaluation.json',
  ),
  reportPath,
} = {}) {
  if (!resultsDirectory) {
    throw new Error('resultsDirectory is required');
  }
  const [scenarios, results, baseline, digest] = await Promise.all([
    loadScenarios(scenariosDirectory),
    loadResults(resultsDirectory),
    readJsonFile(baselinePath, 'baseline'),
    computeInputDigest({ repositoryRoot, scenariosDirectory }),
  ]);
  const report = await evaluateResults({
    scenarios,
    results,
    baseline,
    digest,
  });
  if (reportPath) {
    await writeJsonFile(reportPath, report);
  }
  return report;
}

export async function main(args = process.argv.slice(2)) {
  const options = parseCliOptions(args);
  const resultsDirectory = resolveFrom(
    REPOSITORY_ROOT,
    options.results,
    'tests/fixtures/results',
  );
  const baselinePath = resolveFrom(
    REPOSITORY_ROOT,
    options.baseline,
    'tests/baseline/rca-evaluation.json',
  );
  const reportPath = options.report
    ? resolveFrom(REPOSITORY_ROOT, options.report)
    : undefined;
  const report = await runEvaluation({
    resultsDirectory,
    baselinePath,
    reportPath,
  });

  if (!report.passed) {
    throw new Error(
      `RCA evaluation failed:\n- ${report.failures.join('\n- ')}`,
    );
  }
  process.stdout.write(
    `RCA evaluation passed: ${report.evaluations.length} engine/scenario result(s), digest ${report.inputDigest}\n`,
  );
}

if (isMain(import.meta.url)) {
  main().catch((error) => {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  });
}
