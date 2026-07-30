import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';

import { REPOSITORY_ROOT } from './evaluator.mjs';

async function readRepositoryFile(relativePath) {
  return readFile(path.join(REPOSITORY_ROOT, relativePath), 'utf8');
}

const EXECUTION_MODULE = 'packages/dashboard/server/utils/execution.ts';

// Recovery is no longer something analysis reports on. It is a separate
// lifecycle a person approves, so what the dashboard must get right is the
// approval gate and the execution state — not a remediation field merged into
// the session record.

test('the dashboard cannot publish an approval that the worker would reject', async () => {
  const source = await readRepositoryFile(
    'packages/dashboard/server/api/executions.post.ts',
  );

  // Publishing here is the approval. Every precondition the worker enforces has
  // to be checked before the message exists, or the gate would let through a
  // request that fails after the fact and reads as an approved-but-broken run.
  assert.match(source, /statusCode: 400[\s\S]*?Missing rcaId/);
  assert.match(source, /ALLOWED_ENGINES\.has\(engine\)/);
  assert.match(source, /state !== 'COMPLETED'/);
  assert.match(source, /declares no playbook execution steps/);
  assert.match(source, /An execution is already/);

  // No queue URL must fail loudly: a dashboard that silently skipped publishing
  // would look like it approved something.
  assert.match(source, /statusCode: 503/);
  assert.match(source, /EXECUTION_QUEUE_URL is not configured/);
});

test('an approval carries a stable identifier so a resubmit cannot double-execute', async () => {
  const source = await readRepositoryFile(
    'packages/dashboard/server/api/executions.post.ts',
  );

  // The worker derives the execution id from approval_id, so the same approval
  // claims the same execution instead of starting a second one.
  assert.match(source, /approval_id: approvalId/);
  assert.match(source, /rca_id: rcaId/);
  assert.match(source, /engine,/);
});

test('a retrospective revision becomes the procedure the next execution runs', async () => {
  const source = await readRepositoryFile(
    'packages/dashboard/server/api/executions.post.ts',
  );
  const playbookApi = await readRepositoryFile(
    'packages/dashboard/server/api/playbooks/[id].get.ts',
  );

  // Showing the pre-retrospective steps while running the revised ones would
  // mean a person approves a procedure that is not the one that executes.
  assert.ok(
    source.indexOf('PLAYBOOK_REVISION') < source.indexOf("span_type === 'PLAYBOOK'"),
    'the approval check prefers the revision over the original span',
  );
  assert.ok(
    playbookApi.indexOf('PLAYBOOK_REVISION') <
      playbookApi.indexOf('metadata[field]'),
    'the playbook API prefers the revision over the original span',
  );
});

test('the dashboard reports execution state without altering the analysis session', async () => {
  const [executionModule, sessionsSource, tracesSource] = await Promise.all([
    readRepositoryFile(EXECUTION_MODULE),
    readRepositoryFile('packages/dashboard/server/api/sessions.get.ts'),
    readRepositoryFile('packages/dashboard/server/api/traces/[id].get.ts'),
  ]);

  // The seven states are the worker's lifecycle. A label missing here would
  // render as blank rather than as the state the worker actually recorded.
  for (const state of [
    'PENDING_APPROVAL',
    'EXECUTING',
    'VERIFYING',
    'RESOLVED',
    'UNRESOLVED',
    'FAILED',
    'CANCELLED',
  ]) {
    assert.ok(
      executionModule.includes(state),
      `the execution normalizer knows the ${state} state`,
    );
  }

  // An execution failure must not make a finished analysis look failed, so the
  // execution fields are attached to the row rather than spread into it.
  for (const [name, source] of [
    ['sessions', sessionsSource],
    ['traces', tracesSource],
  ]) {
    assert.match(
      source,
      /isExecutionItem/,
      `${name} API separates execution items from session items`,
    );
    assert.doesNotMatch(
      source,
      /\.\.\.\s*execution\b/,
      `${name} API does not merge execution fields into the session record`,
    );
  }

  assert.match(sessionsSource, /executionState/);
  assert.match(tracesSource, /executions/);
});

test('an unconfirmed resolution is never presented as resolved', async () => {
  const [executionModule, indexSource, reportPage] = await Promise.all([
    readRepositoryFile(EXECUTION_MODULE),
    readRepositoryFile('packages/dashboard/app/pages/index.vue'),
    readRepositoryFile('packages/dashboard/app/pages/report/[id].vue'),
  ]);

  // resolutionConfirmed is tri-state: null means observation could not confirm
  // resolution either way, which is not the same as confirmed false.
  assert.match(executionModule, /resolutionConfirmed: boolean \| null/);
  assert.match(executionModule, /readTristate/);

  // UNRESOLVED and FAILED must not read as success at a glance.
  for (const [name, source] of [
    ['index', indexSource],
    ['report', reportPage],
  ]) {
    assert.match(
      source,
      /state === 'RESOLVED'\) return 'badge-success'/,
      `${name} page marks only RESOLVED as success`,
    );
    assert.match(
      source,
      /'UNRESOLVED' \|\| state === 'FAILED'\) return 'badge-error'/,
      `${name} page marks unresolved and failed as errors`,
    );
  }
});

test('the report page gates approval on a confirmed procedure', async () => {
  const source = await readRepositoryFile(
    'packages/dashboard/app/pages/report/[id].vue',
  );

  // A person approves the procedure while reading the analysis that produced it,
  // so the steps are rendered here rather than on a separate page.
  assert.match(source, /executionSteps/);
  assert.match(source, /step\.success_criteria/);
  assert.match(source, /verification_status/);

  // Approval requires a completed analysis, steps to run, and nothing already
  // running — the same conditions the API enforces.
  assert.match(source, /session\.state === 'COMPLETED'/);
  assert.match(source, /executionSteps\.value\.length > 0/);
  assert.match(source, /!inFlight\.value/);
  assert.match(source, /:disabled="!canApprove"/);

  // Writing starts only after an explicit confirmation.
  assert.match(source, /approvalModal\?\.showModal\(\)/);
  assert.match(source, /되돌릴 수 없는 조치는 서버가 거부/);
});

test('the retrospective view returns all four things its update must be read against', async () => {
  const [api, page] = await Promise.all([
    readRepositoryFile(
      'packages/dashboard/server/api/retrospectives/[rcaId]/[executionId].get.ts',
    ),
    readRepositoryFile(
      'packages/dashboard/app/pages/retrospective/[rcaId]/[executionId].vue',
    ),
  ]);

  // Read apart, none of the four tells you whether the automatic update was
  // justified. The pre-execution copy in particular cannot be recovered later.
  for (const part of ['issue', 'playbookBefore', 'evidence', 'diff']) {
    assert.ok(api.includes(part), `the API returns ${part}`);
    assert.ok(page.includes(part), `the page renders ${part}`);
  }

  // A cleaned-up object shows as a gap rather than failing the whole request.
  assert.match(api, /return null;/);
  assert.match(page, /보존 기간을 지나 조회할 수 없습니다/);

  // Deletion never happens, so what survived is shown alongside what changed.
  assert.match(page, /preserved_steps/);
});

test('the analysis lifecycle no longer contains a recovery stage', async () => {
  const [indexSource, tracePage, graphSource] = await Promise.all([
    readRepositoryFile('packages/dashboard/app/pages/index.vue'),
    readRepositoryFile('packages/dashboard/app/pages/trace/[id].vue'),
    readRepositoryFile('packages/dashboard/app/composables/useTraceGraph.ts'),
  ]);

  // Analysis produces a report and stops. A REMEDIATION stage in the lifecycle
  // would tell an operator that recovery happens without their approval.
  for (const [name, source] of [
    ['index', indexSource],
    ['trace page', tracePage],
    ['trace graph', graphSource],
  ]) {
    assert.doesNotMatch(
      source,
      /REMEDIATION/,
      `${name} has no remediation pipeline stage`,
    );
  }
});

test('dashboard session and trace reads paginate DynamoDB results', async () => {
  const [sessionsSource, tracesSource] = await Promise.all([
    readRepositoryFile('packages/dashboard/server/api/sessions.get.ts'),
    readRepositoryFile('packages/dashboard/server/api/traces/[id].get.ts'),
  ]);

  for (const [name, source] of [
    ['sessions', sessionsSource],
    ['traces', tracesSource],
  ]) {
    assert.match(source, /const items = \[\]/, `${name} accumulates pages`);
    assert.match(
      source,
      /ExclusiveStartKey: exclusiveStartKey/,
      `${name} forwards the page cursor`,
    );
    assert.match(
      source,
      /items\.push\(\.\.\.\(result\.Items \?\? \[\]\)\)/,
      `${name} preserves every page`,
    );
    assert.match(
      source,
      /exclusiveStartKey = result\.LastEvaluatedKey/,
      `${name} reads the next cursor`,
    );
    assert.match(
      source,
      /while \(exclusiveStartKey\)/,
      `${name} continues through the final page`,
    );
  }

  assert.ok(
    tracesSource.indexOf('const items = []') <
      tracesSource.indexOf('function matchesEngine'),
    'trace engine filtering occurs after all pages are accumulated',
  );
});

test('dashboard state graph selects the engine-specific lifecycle', async () => {
  const [graphSource, tracePageSource] = await Promise.all([
    readRepositoryFile('packages/dashboard/app/components/StateGraph.vue'),
    readRepositoryFile('packages/dashboard/app/pages/trace/[id].vue'),
  ]);

  assert.match(graphSource, /engine: string/);
  assert.match(
    graphSource,
    /CC_HEADLESS_HAPPY_PATH = \['ALARM_RECEIVED', 'ANALYZING', 'COMPLETED'\]/,
  );
  assert.match(
    graphSource,
    /props\.engine === 'cc-headless' \? CC_HEADLESS_HAPPY_PATH : STRANDS_HAPPY_PATH/,
  );
  assert.match(graphSource, /ANALYZING: \['COMPLETED'\]/);
  assert.match(tracePageSource, /:engine="trace\.session\.engine"/);
});

test('dashboard cancellation is scoped to the selected engine', async () => {
  const [endpointSource, indexSource] = await Promise.all([
    readRepositoryFile(
      'packages/dashboard/server/api/sessions/[id]/cancel.post.ts',
    ),
    readRepositoryFile('packages/dashboard/app/pages/index.vue'),
  ]);

  assert.match(endpointSource, /const query = getQuery\(event\)/);
  assert.match(endpointSource, /ALLOWED_ENGINES\.has\(engine\)/);
  assert.match(
    endpointSource,
    /engine === 'strands'\s*\?\s*\['strands#SESSION', 'SESSION'\]/,
    'legacy bare SESSION is only a Strands fallback',
  );
  assert.match(
    endpointSource,
    /Key: \{ PK: `RCA#\$\{id\}`, SK: sessionKey \}/,
    'one resolved session key is cancelled',
  );
  assert.match(
    endpointSource,
    /const prefix = sessionKey === 'SESSION' \? 'HYPO#' : `\$\{engine\}#HYPO#`/,
    'only hypotheses belonging to the selected session representation are closed',
  );
  assert.doesNotMatch(
    endpointSource,
    /const engines = \['strands', 'cc-headless'\]/,
  );

  assert.match(
    indexSource,
    /cancelTarget = ref<\{ rcaId: string; engine: string \} \| null>/,
  );
  assert.match(
    indexSource,
    /openCancelModal\(session\.rcaId, session\.engine\)/,
  );
  assert.match(indexSource, /query: \{ engine: cancelTarget\.value\.engine \}/);
});
