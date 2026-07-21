import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { parseArgs } from 'node:util';

export function parseCliOptions(args, { requireResults = false } = {}) {
  const { values } = parseArgs({
    args,
    options: {
      baseline: { type: 'string' },
      report: { type: 'string' },
      results: { type: 'string' },
    },
    strict: true,
  });
  if (requireResults && !values.results) {
    throw new Error(
      'Missing --results <directory>. Pass a reviewed live result directory explicitly.',
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

export async function writeJsonFileAtomically(filePath, value) {
  await writeTextFileAtomically(
    filePath,
    `${JSON.stringify(value, null, 2)}\n`,
  );
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
