import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { parseArgs } from 'node:util';

import {
  computeInputDigest,
  createPendingBaseline,
  REPOSITORY_ROOT,
  validateBaseline,
} from './evaluator.mjs';
import {
  isMain,
  serializeBaseline,
  writeTextFileAtomically,
} from './cli-utils.mjs';

/** Record reviewed input identity without awarding model approval. */
export async function syncInputBaseline({
  repositoryRoot = REPOSITORY_ROOT,
  scenariosDirectory = path.join(repositoryRoot, 'tests/scenarios'),
  baselinePath = path.join(
    repositoryRoot,
    'tests/baseline/rca-evaluation.json',
  ),
  updatedAt,
} = {}) {
  const digest = await computeInputDigest({
    repositoryRoot,
    scenariosDirectory,
  });
  let previousBytes;
  let previous;
  try {
    previousBytes = await readFile(baselinePath);
    previous = JSON.parse(previousBytes.toString('utf8'));
    validateBaseline(previous);
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }

  if (
    previous?.inputDigest === digest.digest &&
    JSON.stringify(previous.inputFiles) === JSON.stringify(digest.inputFiles)
  ) {
    return previous;
  }

  if (previous) {
    // Content-addressed, append-only history: neither a second sync nor a file
    // name collision may replace the bytes of a past approval.
    const history = path.join(path.dirname(baselinePath), 'history');
    const hash = createHash('sha256').update(previousBytes).digest('hex');
    const archive = path.join(history, `${hash}.json`);
    await mkdir(history, { recursive: true });
    try {
      await writeFile(archive, previousBytes, { flag: 'wx' });
    } catch (error) {
      if (error.code !== 'EEXIST') throw error;
      if (!(await readFile(archive)).equals(previousBytes)) {
        throw new Error('baseline history collision; refusing to overwrite');
      }
    }
  }

  const pending = createPendingBaseline({ digest, updatedAt });
  await writeTextFileAtomically(baselinePath, serializeBaseline(pending));
  return pending;
}

export async function main(args = process.argv.slice(2)) {
  const { values } = parseArgs({
    args,
    options: { baseline: { type: 'string' } },
    strict: true,
  });
  const baseline = await syncInputBaseline({
    baselinePath: values.baseline
      ? path.resolve(REPOSITORY_ROOT, values.baseline)
      : undefined,
  });
  process.stdout.write(
    `Input baseline ${baseline.inputDigest}; model approval ${baseline.status ?? 'approved'}\n`,
  );
}

if (isMain(import.meta.url)) {
  main().catch((error) => {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  });
}
