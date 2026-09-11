// Historical fixtures exercise lifecycle policy only: these are not reviewed
// results for today's scenarios. Every baseline write targets a temporary path.
import assert from 'node:assert/strict';
import {
  mkdtemp,
  readFile,
  readdir,
  rm,
  stat,
  writeFile,
} from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test, { before } from 'node:test';

import { main as evaluateCli } from './eval-cli.mjs';
import {
  computeInputDigest,
  createBaseline,
  createPendingBaseline,
  DEFAULT_CONTRACT_INPUTS,
  evaluateResults,
  EXPECTED_ENGINES,
  loadResults,
  loadScenarios,
  REPOSITORY_ROOT,
  validateBaseline,
} from './evaluator.mjs';
import { syncInputBaseline } from './sync-baseline-cli.mjs';

const UPDATED = '2026-09-11T00:00:00.000Z';
const LATER = '2026-09-11T01:00:00.000Z';
let digest;
let scenarios;
let results;
let passingReport;

before(async () => {
  const historical = path.join(
    REPOSITORY_ROOT,
    'tests/fixtures/historical/original-four',
  );
  [digest, scenarios, results] = await Promise.all([
    computeInputDigest(),
    loadScenarios(path.join(historical, 'scenarios')),
    loadResults(path.join(historical, 'results')),
  ]);
  passingReport = await evaluateResults({ scenarios, results, digest });
  assert.equal(passingReport.passed, true, passingReport.failures.join('\n'));
});

async function temporaryBaseline(t) {
  const directory = await mkdtemp(
    path.join(tmpdir(), 'rca-baseline-lifecycle-'),
  );
  t.after(() => rm(directory, { recursive: true, force: true }));
  return path.join(directory, 'baseline.json');
}

function approved(version = 3) {
  const baseline = createBaseline({
    report: passingReport,
    digest,
    approvedAt: UPDATED,
  });
  if (version === 3) return baseline;
  // Exact historical v2 shape, without inventing optional v3 fields.
  return {
    schemaVersion: 2,
    approvedAt: baseline.approvedAt,
    engines: baseline.engines,
    contractInputs: baseline.contractInputs,
    inputDigest: baseline.inputDigest,
    inputFiles: baseline.inputFiles,
  };
}

async function readBaseline(baselinePath) {
  return JSON.parse(await readFile(baselinePath, 'utf8'));
}

async function historyBytes(baselinePath) {
  const directory = path.join(path.dirname(baselinePath), 'history');
  async function collect(current) {
    const entries = await readdir(current, { withFileTypes: true }).catch(
      (error) => {
        if (error.code === 'ENOENT') return [];
        throw error;
      },
    );
    return (
      await Promise.all(
        entries.map((entry) => {
          const file = path.join(current, entry.name);
          return entry.isDirectory() ? collect(file) : readFile(file);
        }),
      )
    ).flat();
  }
  return collect(directory);
}

async function assertUnchanged(baselinePath, bytes, metadata) {
  assert.deepEqual(await readFile(baselinePath), bytes);
  const after = await stat(baselinePath, { bigint: true });
  assert.equal(
    after.ino,
    metadata.ino,
    'same identity must not replace the file',
  );
  assert.equal(
    after.mtimeNs,
    metadata.mtimeNs,
    'same identity must not rewrite it',
  );
}

test('legacy v2 exact shape remains approved and rejects extra fields', async () => {
  const baseline = approved(2);
  validateBaseline(baseline);
  const report = await evaluateResults({
    scenarios,
    results,
    digest,
    baseline,
  });
  assert.equal(report.baselineStatus, 'approved');
  assert.equal(report.passed, true);
  assert.throws(() => validateBaseline({ ...baseline, status: 'approved' }));
});

test('v3 metadata distinguishes pending from approved and requires nonblank timestamp strings', () => {
  const baseline = createPendingBaseline({ digest, updatedAt: UPDATED });
  validateBaseline(baseline);
  assert.deepEqual(baseline, {
    schemaVersion: 3,
    status: 'pending',
    updatedAt: UPDATED,
    approvedAt: null,
    engines: EXPECTED_ENGINES,
    contractInputs: DEFAULT_CONTRACT_INPUTS,
    inputDigest: digest.digest,
    inputFiles: digest.inputFiles,
  });
  for (const patch of [
    { status: 'unknown' },
    { status: 'approved', approvedAt: null },
    { approvedAt: UPDATED },
    { updatedAt: '' },
    { updatedAt: '   ' },
    { updatedAt: null },
    { status: 'approved', approvedAt: '' },
    { status: 'approved', approvedAt: '   ' },
    { extra: 'unrecognized approval metadata' },
  ]) {
    assert.throws(
      () => validateBaseline({ ...baseline, ...patch }),
      `invalid v3 metadata must be rejected: ${JSON.stringify(patch)}`,
    );
  }
});

test('matching pending baseline cannot pass even when every historical gate passes', async () => {
  assert.equal(passingReport.baselineStatus, null);
  const baseline = createPendingBaseline({ digest, updatedAt: UPDATED });
  const report = await evaluateResults({
    scenarios,
    results,
    digest,
    baseline,
  });
  assert.equal(report.digestMatches, true);
  assert.equal(report.baselineStatus, 'pending');
  assert.ok(report.evaluations.every((evaluation) => evaluation.passed));
  assert.equal(report.passed, false);
  assert.ok(
    report.failures.some((failure) => /pending|approval/i.test(failure)),
  );
  assert.throws(() => createBaseline({ report, digest, approvedAt: LATER }));
  const drifted = await evaluateResults({
    scenarios,
    results,
    digest,
    baseline: { ...baseline, inputDigest: `sha256:${'0'.repeat(64)}` },
  });
  assert.equal(drifted.passed, false);
  assert.equal(drifted.digestMatches, false);
  assert.ok(drifted.failures.some((failure) => /digest drifted/.test(failure)));
});

test('full passing unit report approves v3 while digest and file drift still fail', async () => {
  const baseline = approved();
  validateBaseline(baseline);
  assert.equal(baseline.schemaVersion, 3);
  assert.equal(baseline.status, 'approved');
  assert.equal(baseline.updatedAt, UPDATED);
  assert.equal(baseline.approvedAt, UPDATED);
  const report = await evaluateResults({
    scenarios,
    results,
    digest,
    baseline,
  });
  assert.equal(report.passed, true);
  assert.equal(report.baselineStatus, 'approved');
  for (const patch of [
    { inputDigest: `sha256:${'0'.repeat(64)}` },
    { inputFiles: [...baseline.inputFiles, 'retired-contract.md'] },
  ]) {
    const drifted = await evaluateResults({
      scenarios,
      results,
      digest,
      baseline: { ...baseline, ...patch },
    });
    assert.equal(drifted.passed, false);
    assert.ok(drifted.failures.some((failure) => /drift/i.test(failure)));
  }
});

test('failed mandatory gates and passing partial engine reports cannot approve', async () => {
  const failedResults = structuredClone(results);
  failedResults[0].remediation.safe = false;
  const failed = await evaluateResults({
    scenarios,
    results: failedResults,
    digest,
  });
  assert.equal(failed.passed, false);
  assert.ok(failed.failures.some((failure) => /mandatory gate/.test(failure)));
  assert.throws(() => createBaseline({ report: failed, digest }));
  const engine = EXPECTED_ENGINES[0];
  const partial = await evaluateResults({
    scenarios,
    results: results.filter((result) => result.engine === engine),
    digest,
    engines: [engine],
  });
  assert.equal(partial.passed, true);
  assert.equal(partial.enginesComplete, false);
  assert.throws(() => createBaseline({ report: partial, digest }));
  const missing = await evaluateResults({
    scenarios,
    results: results.slice(1),
    digest,
  });
  assert.equal(missing.passed, false);
  assert.throws(() => createBaseline({ report: missing, digest }));
});

test('first synchronization records the current repository digest as pending only', async (t) => {
  const baselinePath = await temporaryBaseline(t);
  await syncInputBaseline({ baselinePath, updatedAt: UPDATED });
  const baseline = await readBaseline(baselinePath);
  assert.deepEqual(
    baseline,
    createPendingBaseline({ digest, updatedAt: UPDATED }),
  );
  validateBaseline(baseline);
  assert.deepEqual(await historyBytes(baselinePath), []);
  assert.deepEqual(await readdir(path.dirname(baselinePath)), [
    'baseline.json',
  ]);
});

for (const version of [2, 3]) {
  test(`changed approved v${version} is archived byte-exact before pending refresh`, async (t) => {
    const baselinePath = await temporaryBaseline(t);
    const old = {
      ...approved(version),
      inputDigest: `sha256:${'f'.repeat(64)}`,
    };
    // Deliberate whitespace/line endings distinguish a byte copy from reserialization.
    const bytes = Buffer.from(`${JSON.stringify(old, null, '\t')}\r\n`);
    await writeFile(baselinePath, bytes);
    const history = path.join(path.dirname(baselinePath), 'history');
    await writeFile(history, 'local obstruction to archive creation');
    await assert.rejects(syncInputBaseline({ baselinePath, updatedAt: LATER }));
    assert.deepEqual(
      await readFile(baselinePath),
      bytes,
      'archive failure must leave the original approval untouched',
    );
    await rm(history);
    await syncInputBaseline({ baselinePath, updatedAt: LATER });
    assert.deepEqual(
      await readBaseline(baselinePath),
      createPendingBaseline({ digest, updatedAt: LATER }),
    );
    assert.deepEqual(await historyBytes(baselinePath), [bytes]);
    const pendingBytes = await readFile(baselinePath);
    const metadata = await stat(baselinePath, { bigint: true });
    await syncInputBaseline({ baselinePath, updatedAt: UPDATED });
    await assertUnchanged(baselinePath, pendingBytes, metadata);
    assert.deepEqual(await historyBytes(baselinePath), [bytes]);
  });
}

test('matching approved v2 and v3 remain approved without rewriting or archiving', async (t) => {
  for (const version of [2, 3]) {
    const baselinePath = await temporaryBaseline(t);
    const bytes = Buffer.from(
      `${JSON.stringify(approved(version), null, '\t')}\n`,
    );
    await writeFile(baselinePath, bytes);
    const metadata = await stat(baselinePath, { bigint: true });
    await syncInputBaseline({ baselinePath, updatedAt: LATER });
    await assertUnchanged(baselinePath, bytes, metadata);
    assert.deepEqual(await historyBytes(baselinePath), []);
    const report = await evaluateResults({
      scenarios,
      results,
      digest,
      baseline: await readBaseline(baselinePath),
    });
    assert.equal(report.baselineStatus, 'approved');
    assert.equal(report.passed, true);
  }
});

test('repeated matching pending synchronization cannot change approval or timestamps', async (t) => {
  const baselinePath = await temporaryBaseline(t);
  await syncInputBaseline({ baselinePath, updatedAt: UPDATED });
  const bytes = await readFile(baselinePath);
  const metadata = await stat(baselinePath, { bigint: true });
  await syncInputBaseline({ baselinePath, updatedAt: LATER });
  await assertUnchanged(baselinePath, bytes, metadata);
  assert.deepEqual(await historyBytes(baselinePath), []);
  const baseline = await readBaseline(baselinePath);
  assert.equal(baseline.status, 'pending');
  assert.equal(baseline.approvedAt, null);
});

test('same digest with a changed input file set is a new identity requiring pending', async (t) => {
  const baselinePath = await temporaryBaseline(t);
  const bytes = Buffer.from(
    JSON.stringify({
      ...approved(),
      inputFiles: [...digest.inputFiles, 'retired-contract.md'],
    }),
  );
  await writeFile(baselinePath, bytes);
  await syncInputBaseline({ baselinePath, updatedAt: LATER });
  const baseline = await readBaseline(baselinePath);
  assert.equal(baseline.status, 'pending');
  assert.equal(baseline.approvedAt, null);
  assert.equal(baseline.inputDigest, digest.digest);
  assert.deepEqual(baseline.inputFiles, digest.inputFiles);
  assert.deepEqual(await historyBytes(baselinePath), [bytes]);
});

test('offline CLI still rejects pending and remains required by pnpm verify', async (t) => {
  const baselinePath = await temporaryBaseline(t);
  await syncInputBaseline({ baselinePath, updatedAt: UPDATED });
  await assert.rejects(
    evaluateCli(['--baseline', baselinePath]),
    /RCA evaluation failed/,
  );
  const { scripts } = JSON.parse(
    await readFile(path.join(REPOSITORY_ROOT, 'package.json'), 'utf8'),
  );
  assert.match(scripts.verify, /pnpm run test/);
  assert.match(scripts.test, /pnpm run eval:offline/);
  assert.equal(scripts['eval:offline'], 'node tests/harness/eval-cli.mjs');
});
