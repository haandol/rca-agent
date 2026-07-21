import path from 'node:path';

import {
  computeInputDigest,
  createBaseline,
  evaluateResults,
  loadResults,
  loadScenarios,
  REPOSITORY_ROOT,
} from './evaluator.mjs';
import {
  isMain,
  parseCliOptions,
  resolveFrom,
  writeTextFileAtomically,
} from './cli-utils.mjs';

function serializeBaseline(baseline) {
  const expandedEngines = `  "engines": ${JSON.stringify(
    baseline.engines,
    null,
    2,
  ).replaceAll('\n', '\n  ')},`;
  const compactEngines = `  "engines": [${baseline.engines
    .map((engine) => JSON.stringify(engine))
    .join(', ')}],`;
  return `${JSON.stringify(baseline, null, 2).replace(
    expandedEngines,
    compactEngines,
  )}\n`;
}

export async function approveBaseline({
  repositoryRoot = REPOSITORY_ROOT,
  scenariosDirectory = path.join(repositoryRoot, 'tests/scenarios'),
  resultsDirectory,
  baselinePath = path.join(
    repositoryRoot,
    'tests/baseline/rca-evaluation.json',
  ),
  approvedAt,
} = {}) {
  if (!resultsDirectory) {
    throw new Error('resultsDirectory is required for baseline approval');
  }
  const [scenarios, results, digest] = await Promise.all([
    loadScenarios(scenariosDirectory),
    loadResults(resultsDirectory),
    computeInputDigest({ repositoryRoot, scenariosDirectory }),
  ]);
  const report = await evaluateResults({ scenarios, results, digest });
  const baseline = createBaseline({ report, digest, approvedAt });
  await writeTextFileAtomically(baselinePath, serializeBaseline(baseline));
  return baseline;
}

export async function main(args = process.argv.slice(2)) {
  const options = parseCliOptions(args, { requireResults: true });
  const resultsDirectory = resolveFrom(REPOSITORY_ROOT, options.results);
  const baselinePath = resolveFrom(
    REPOSITORY_ROOT,
    options.baseline,
    'tests/baseline/rca-evaluation.json',
  );
  const baseline = await approveBaseline({ resultsDirectory, baselinePath });
  process.stdout.write(
    `Approved RCA baseline at ${baselinePath} with digest ${baseline.inputDigest}\n`,
  );
}

if (isMain(import.meta.url)) {
  main().catch((error) => {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  });
}
