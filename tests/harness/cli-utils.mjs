import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { parseArgs } from 'node:util';

export function parseCliOptions(
  args,
  { requireResults = false, allowEngine = false } = {},
) {
  const { values } = parseArgs({
    args,
    options: {
      baseline: { type: 'string' },
      report: { type: 'string' },
      results: { type: 'string' },
      // Repeatable so one run can cover a subset of engines: a real-model round
      // costs tens of minutes per engine, and re-running a passing engine only
      // because another one's environment was misconfigured wastes that round.
      ...(allowEngine ? { engine: { type: 'string', multiple: true } } : {}),
    },
    strict: true,
  });
  if (requireResults && !values.results) {
    throw new Error(
      'Missing --results <directory>. Pass a reviewed model-eval result directory explicitly.',
    );
  }
  return values;
}

export async function readJsonFile(filePath, label) {
  try {
    return JSON.parse(await readFile(filePath, 'utf8'));
  } catch (error) {
    throw new Error(
      `Unable to read ${label} JSON at ${filePath}: ${error.message}`,
      {
        cause: error,
      },
    );
  }
}

export async function writeJsonFile(filePath, value) {
  await mkdir(path.dirname(filePath), { recursive: true });
  await writeFile(filePath, `${JSON.stringify(value, null, 2)}\n`, 'utf8');
}

export async function writeTextFileAtomically(filePath, content) {
  const temporaryPath = `${filePath}.${process.pid}.tmp`;
  await mkdir(path.dirname(filePath), { recursive: true });
  await writeFile(temporaryPath, content, 'utf8');
  await rename(temporaryPath, filePath);
}

export function resolveFrom(root, candidate, fallback) {
  return path.resolve(root, candidate ?? fallback);
}

export function isMain(importMetaUrl) {
  return (
    process.argv[1] && importMetaUrl === pathToFileURL(process.argv[1]).href
  );
}
