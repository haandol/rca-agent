import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';
import { pathToFileURL } from 'node:url';

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
  assert.match(source, /isAllowedEngine\(engine\)/);
  assert.match(source, /state !== 'COMPLETED'/);
  // The refusals are asserted on the condition each one guards rather than on its
  // wording: these sentences are read by a person in a dialog and get rewritten,
  // while the three conditions are the contract the worker also enforces.
  assert.match(source, /!steps\.length/);
  assert.match(source, /if \(running\)/);
  assert.equal(
    source.match(/statusCode: 409/g)?.length,
    3,
    'each of the three approval conditions refuses with a 409',
  );

  // No queue URL must fail loudly: a dashboard that silently skipped publishing
  // would look like it approved something.
  assert.match(source, /statusCode: 503/);
  // The refusal names the variable, so whoever reads it can fix the deployment
  // rather than only learning that approval failed.
  assert.match(source, /EXECUTION_QUEUE_URL/);
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
    source.indexOf('PLAYBOOK_REVISION') <
      source.indexOf("span_type === 'PLAYBOOK'"),
    'the approval check prefers the revision over the original span',
  );
  assert.ok(
    playbookApi.indexOf('PLAYBOOK_REVISION') <
      playbookApi.indexOf('metadata[field]'),
    'the playbook API prefers the revision over the original span',
  );
});

test('a person deciding to approve can tell a proven procedure from a draft', async () => {
  const [playbookApi, reportPage] = await Promise.all([
    readRepositoryFile('packages/dashboard/server/api/playbooks/[id].get.ts'),
    readRepositoryFile('packages/dashboard/app/pages/report/[id].vue'),
  ]);

  // The promotion is recorded on the revision, so reading the status off the
  // analysis span alone would show a draft forever.
  assert.match(
    playbookApi,
    /verification_status: text\('verification_status'\)/,
    'the playbook API must read the status through the revision-first accessor',
  );

  // Anything other than the recorded VERIFIED has to read as a draft: an
  // unproven procedure looking proven is what misleads the approver.
  assert.match(reportPage, /verification_status === 'VERIFIED'/);
  assert.match(
    reportPage,
    /검증된 절차/,
    'the verified state must be named in words rather than printed as the enum',
  );
  assert.match(
    reportPage,
    /초안/,
    'the unproven state must be named as a draft',
  );
  assert.doesNotMatch(
    reportPage,
    /\{\{\s*playbook\.verification_status\s*\}\}/,
    'the raw enum must never be rendered directly',
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

  // UNRESOLVED and FAILED must not read as success at a glance. The report page
  // still maps execution states to a tone directly; the session list collapses
  // the analysis and execution lifecycles into one outcome word, so its rule
  // lives in the shared vocabulary and is asserted by executing it below.
  assert.match(
    reportPage,
    /state === 'RESOLVED'\) return 'text-success'/,
    'report page marks only RESOLVED as success',
  );
  assert.match(
    reportPage,
    /'UNRESOLVED' \|\| state === 'FAILED'\) return 'text-error'/,
    'report page marks unresolved and failed as errors',
  );

  // The list derives its single word from the shared module, so it must not
  // reimplement the mapping and drift from it.
  assert.match(
    indexSource,
    /outcomeOf/,
    'the session list derives its outcome from the shared vocabulary',
  );

  const { outcomeOf, OUTCOME_TONE } = await import(
    pathToFileURL(
      path.join(
        REPOSITORY_ROOT,
        'packages/dashboard/app/utils/sessionState.ts',
      ),
    ).href
  );

  const resolved = outcomeOf({
    state: 'COMPLETED',
    readiness: 'EXECUTION_UNDERWAY',
    executionState: 'RESOLVED',
  });
  assert.equal(resolved, 'RESOLVED');
  assert.match(OUTCOME_TONE[resolved], /success/);

  for (const failed of ['UNRESOLVED', 'FAILED', 'CANCELLED']) {
    const outcome = outcomeOf({
      state: 'COMPLETED',
      readiness: 'EXECUTION_UNDERWAY',
      executionState: failed,
    });
    assert.equal(
      outcome,
      'UNRESOLVED',
      `${failed} execution must not read as resolved`,
    );
    assert.doesNotMatch(
      OUTCOME_TONE[outcome],
      /success/,
      `${failed} execution must not be toned as success`,
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
  // running — the same conditions the API enforces. The state check is asserted
  // on the optional-chained form the gate itself uses, so this does not pass on
  // an unrelated mention of the same state elsewhere in the page.
  assert.match(source, /session\.value\?\.state === 'COMPLETED'/);
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

test('no dashboard read drops rows DynamoDB withheld', async () => {
  // DynamoDB truncates a page whenever it feels like it, so every read either
  // exhausts the cursor or hands it onward. Doing neither loses rows silently,
  // which is the failure mode that matters: the page still renders.
  const [tracesSource, summarySource, sessionsSource] = await Promise.all([
    readRepositoryFile('packages/dashboard/server/api/traces/[id].get.ts'),
    readRepositoryFile('packages/dashboard/server/api/sessions-summary.get.ts'),
    readRepositoryFile('packages/dashboard/server/api/sessions.get.ts'),
  ]);

  // These two must be complete to be correct: a trace is one run's whole history,
  // and the archive counts describe everything. Both loop until the cursor clears.
  for (const [name, source] of [
    ['traces', tracesSource],
    ['summary', summarySource],
  ]) {
    assert.match(
      source,
      /ExclusiveStartKey:/,
      `${name} forwards the page cursor`,
    );
    assert.match(
      source,
      /= result\.LastEvaluatedKey/,
      `${name} reads the next cursor`,
    );
    assert.match(source, /while \(/, `${name} continues through the last page`);
  }

  assert.ok(
    tracesSource.indexOf('const items = []') <
      tracesSource.indexOf('function matchesEngine'),
    'trace engine filtering occurs after all pages are accumulated',
  );

  // The list is deliberately one page, so instead of exhausting the cursor it
  // returns one. Truncating without saying so would make an archive with more
  // rows look finished.
  assert.match(
    sessionsSource,
    /nextCursor: encodeCursor\(/,
    'the session list hands its position back to the caller',
  );
  assert.doesNotMatch(
    sessionsSource,
    /while \(exclusiveStartKey\)/,
    'the session list must not walk the whole table it is paging',
  );

  // Reading sessions through the index is what makes a page cheap: the table
  // holds roughly seven trace items per session, and a scan reads them all.
  assert.match(
    sessionsSource,
    /IndexName: SESSION_LIST_INDEX/,
    'the session list reads the session index rather than scanning',
  );
  assert.doesNotMatch(
    sessionsSource,
    /ScanCommand/,
    'the session list no longer scans',
  );
  assert.match(
    sessionsSource,
    /ScanIndexForward: false/,
    'the list reads newest first, which a scan cannot promise',
  );
});

test('the session index stays session-only, and old sessions are backfilled into it', async () => {
  const [cdkSource, indexModule, backfill] = await Promise.all([
    readRepositoryFile('packages/infra/lib/stacks/database-stack.ts'),
    readRepositoryFile('packages/dashboard/server/utils/sessionIndex.ts'),
    readRepositoryFile('scripts/backfill_session_list_index.py'),
  ]);

  // The keys must be attributes only a session carries. `engine` and `created_at`
  // are also on hypothesis and execution items, so reusing them would pull those
  // into the index — measured at seven times the session count, which makes a page
  // of 25 come back mostly hypotheses.
  assert.match(cdkSource, /indexName: 'session-by-engine-index'/);
  assert.match(cdkSource, /name: 'list_engine'/);
  assert.match(cdkSource, /name: 'list_created_at'/);
  assert.match(indexModule, /LIST_PARTITION_KEY = 'list_engine'/);
  assert.match(indexModule, /LIST_SORT_KEY = 'list_created_at'/);

  // Both engines write the keys, or that engine's sessions never appear.
  for (const enginePath of [
    'packages/agent/src/rca_agent/adapters/secondary/session/dynamodb_session_store.py',
    'packages/cc-headless/src/cc_headless/adapters/secondary/session/dynamodb_session_store.py',
  ]) {
    const source = await readRepositoryFile(enginePath);
    assert.match(source, /"list_engine"/, `${enginePath} writes the index key`);
    assert.match(
      source,
      /"list_created_at"/,
      `${enginePath} writes the index sort key`,
    );
  }

  // Sessions written before the index have no keys and would vanish from the list,
  // so the backfill is part of the same change — and it must not overwrite state
  // a running analysis owns.
  assert.match(backfill, /attribute_not_exists\(/, 'backfill is additive only');
  assert.match(
    backfill,
    /ConditionalCheckFailedException/,
    'a row filled concurrently is not an error',
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
  assert.match(endpointSource, /isAllowedEngine\(engine\)/);
  assert.match(
    endpointSource,
    /Key: \{ PK: rcaPk\(id\), SK: sessionKey \}/,
    'one resolved session key is cancelled',
  );
  assert.doesNotMatch(
    endpointSource,
    /const engines = \['strands', 'cc-headless'\]/,
  );

  // The legacy fallback and the hypothesis scoping are shared key helpers now,
  // so they are exercised directly rather than pinned as handler text — the rule
  // is what the helpers return, not where the handler spells it out.
  const { sessionSkCandidates, hypothesisSkPrefix } = await import(
    pathToFileURL(
      path.join(REPOSITORY_ROOT, 'packages/dashboard/server/utils/keys.ts'),
    ).href
  );

  // Bare `SESSION` predates the engine prefix and is therefore only ever Strands.
  assert.deepEqual(sessionSkCandidates('strands'), [
    'strands#SESSION',
    'SESSION',
  ]);
  assert.deepEqual(sessionSkCandidates('cc-headless'), ['cc-headless#SESSION']);

  // Only hypotheses belonging to the resolved session representation are closed.
  assert.equal(hypothesisSkPrefix('strands', 'SESSION'), 'HYPO#');
  assert.equal(
    hypothesisSkPrefix('strands', 'strands#SESSION'),
    'strands#HYPO#',
  );
  assert.equal(
    hypothesisSkPrefix('cc-headless', 'cc-headless#SESSION'),
    'cc-headless#HYPO#',
  );

  assert.match(
    indexSource,
    /cancelTarget = ref<\{ rcaId: string; engine: string \} \| null>/,
  );
  // Cancelling has to name both the session and the engine: the two engines
  // analyse the same alarm in one partition, so an id alone would fence the wrong
  // run. The row variable's name is the page's business, the pair of arguments is
  // not.
  assert.match(
    indexSource,
    /openCancelModal\(\w+\.rcaId, \w+\.engine\)/,
    'the cancel action passes both the session id and its engine',
  );
  assert.match(
    indexSource,
    /openDeleteModal\(\w+\.rcaId, \w+\.engine\)/,
    'the delete action passes both the session id and its engine',
  );
  assert.match(indexSource, /query: \{ engine: cancelTarget\.value\.engine \}/);
  assert.match(
    indexSource,
    /\?engine=\$\{deleteTarget\.value\.engine\}/,
    'delete forwards the engine so it cannot remove the other engine’s session',
  );
});

test('every DynamoDB expression alias the dashboard uses is declared', async () => {
  // An alias only fails at request time: `state` is a reserved word, so it can
  // only be named through one, and a projection that uses `#st` without declaring
  // it returns a 400 that no type check or unit test would have caught. This walks
  // the handlers the way DynamoDB does — the aliases inside each expression, and
  // the names declared beside it.
  const { readdir } = await import('node:fs/promises');

  const EXPRESSION_KEYS = [
    'KeyConditionExpression',
    'FilterExpression',
    'ProjectionExpression',
    'UpdateExpression',
    'ConditionExpression',
  ];

  async function typescriptFiles(dir) {
    const entries = await readdir(path.join(REPOSITORY_ROOT, dir), {
      withFileTypes: true,
    });
    const files = [];
    for (const entry of entries) {
      const next = `${dir}/${entry.name}`;
      if (entry.isDirectory()) files.push(...(await typescriptFiles(next)));
      else if (entry.name.endsWith('.ts')) files.push(next);
    }
    return files;
  }

  const files = await typescriptFiles('packages/dashboard/server');
  assert.ok(files.length > 0, 'the scan found handlers to check');

  for (const file of files) {
    const source = await readRepositoryFile(file);
    const declared = new Set(
      [...source.matchAll(/'(#[A-Za-z_][A-Za-z0-9_]*)'\s*:/g)].map(
        (match) => match[1],
      ),
    );

    for (const key of EXPRESSION_KEYS) {
      const pattern = new RegExp(
        `${key}\\s*:\\s*((?:'[^']*'|"[^"]*"|\`[^\`]*\`|\\s*\\+\\s*)+)`,
        'g',
      );
      for (const expression of source.matchAll(pattern)) {
        for (const alias of expression[1].matchAll(
          /#[A-Za-z_][A-Za-z0-9_]*/g,
        )) {
          assert.ok(
            declared.has(alias[0]),
            `${file} uses ${alias[0]} in ${key} without declaring it in ExpressionAttributeNames`,
          );
        }
      }
    }
  }
});
