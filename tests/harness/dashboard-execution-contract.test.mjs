import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import path from 'node:path';
import test from 'node:test';
import { pathToFileURL } from 'node:url';

import { REPOSITORY_ROOT } from './evaluator.mjs';

async function readRepositoryFile(relativePath) {
  return readFile(path.join(REPOSITORY_ROOT, relativePath), 'utf8');
}

const EXECUTION_MODULE = 'packages/dashboard/server/utils/execution.ts';
const PLAYBOOK_MODULE = 'packages/dashboard/server/utils/playbook.ts';
const APPROVAL_MODULE = 'packages/dashboard/server/utils/executionApproval.ts';

async function importRepositoryModule(relativePath) {
  return import(pathToFileURL(path.join(REPOSITORY_ROOT, relativePath)).href);
}

// Compile the actual display components without booting Nuxt or contacting AWS.
// Resolve Vue from Nuxt's dependencies, so this works with pnpm's isolated layout.
async function renderRecoveryPlan(props) {
  const dashboardRequire = createRequire(
    path.join(REPOSITORY_ROOT, 'packages/dashboard/package.json'),
  );
  const nuxtRequire = createRequire(
    dashboardRequire.resolve('nuxt/package.json'),
  );
  const { parse, compileScript } = nuxtRequire('vue/compiler-sfc');
  const { createSSRApp, computed } = nuxtRequire('vue');
  const { renderToString } = nuxtRequire('vue/server-renderer');
  const { transpileModule, ModuleKind, ScriptTarget } =
    dashboardRequire('typescript');

  async function component(name) {
    const filename = `packages/dashboard/app/components/${name}.vue`;
    const { descriptor } = parse(await readRepositoryFile(filename), {
      filename,
    });
    const compiled = compileScript(descriptor, {
      id: name,
      inlineTemplate: true,
      templateOptions: { ssr: true },
    });
    const { outputText } = transpileModule(compiled.content, {
      compilerOptions: {
        module: ModuleKind.CommonJS,
        target: ScriptTarget.ES2022,
      },
    });
    const module = { exports: {} };
    new Function('require', 'module', 'exports', 'computed', outputText)(
      nuxtRequire,
      module,
      module.exports,
      computed,
    );
    return module.exports.default;
  }

  const [plan, metrics] = await Promise.all([
    component('RecoveryPlanSteps'),
    component('MetricWaitDetails'),
  ]);
  const app = createSSRApp(plan, props);
  app.component('MetricWaitDetails', metrics);
  return renderToString(app);
}

// Recovery is no longer something analysis reports on. It is a separate
// lifecycle a person approves, so what the dashboard must get right is the
// approval gate and the execution state — not a remediation field merged into
// the session record.

test('the dashboard cannot publish an approval that the worker would reject', async () => {
  const [source, playbookSource, reportSource] = await Promise.all([
    readRepositoryFile('packages/dashboard/server/api/executions.post.ts'),
    readRepositoryFile('packages/dashboard/server/api/playbooks/[id].get.ts'),
    readRepositoryFile('packages/dashboard/server/api/reports/[id].get.ts'),
  ]);

  // Publishing here is the approval. Every precondition the worker enforces has
  // to be checked before the message exists, or the gate would let through a
  // request that fails after the fact and reads as an approved-but-broken run.
  assert.match(source, /statusCode: 400[\s\S]*?Missing rcaId/);
  assert.match(source, /isAllowedEngine\(engine\)/);
  assert.match(source, /isUuid\(approvalId\)/);
  assert.match(source, /state !== 'COMPLETED'/);
  assert.match(source, /session\.confirmed !== true/);
  assert.match(source, /!reportS3Key/);
  assert.match(source, /HeadObjectCommand/);
  assert.match(source, /validateExecutablePlaybook/);
  assert.match(playbookSource, /session\.state !== 'COMPLETED'/);
  assert.match(reportSource, /sessionResult\.Item\?\.state !== 'COMPLETED'/);

  // No queue URL must fail loudly: a dashboard that silently skipped publishing
  // would look like it approved something.
  assert.match(source, /statusCode: 503/);
  // The refusal names the variable, so whoever reads it can fix the deployment
  // rather than only learning that approval failed.
  assert.match(source, /EXECUTION_QUEUE_URL/);
});

test('approval rejects a missing or stale inspected digest before snapshot/reservation/publication', async () => {
  const source = await readRepositoryFile(
    'packages/dashboard/server/api/executions.post.ts',
  );
  const dashboardRequire = createRequire(
    path.join(REPOSITORY_ROOT, 'packages/dashboard/package.json'),
  );
  const { transpileModule, ModuleKind, ScriptTarget } =
    dashboardRequire('typescript');
  const code = transpileModule(source, {
    compilerOptions: {
      module: ModuleKind.CommonJS,
      target: ScriptTarget.ES2022,
    },
  }).outputText;
  const approval = await importRepositoryModule(APPROVAL_MODULE);
  const playbooks = await importRepositoryModule(PLAYBOOK_MODULE);
  const keys = await importRepositoryModule(
    'packages/dashboard/server/utils/keys.ts',
  );
  const book = {
    playbook_id: 'fixed-book',
    execution_steps: [
      {
        step_id: 'stop',
        action: 'Stop observed owner',
        success_criteria: 'stop acknowledged',
        commands: [
          'aws ecs stop-task --cluster demo --task owner --region us-east-1',
        ],
      },
    ],
  };
  const expected = approval.sha256Hex(approval.serializePlaybookSnapshot(book));
  async function run(expectedPlaybookDigest, currentBook = book) {
    const events = [];
    const body = {
      rcaId: 'fixture',
      engine: 'headless-codex',
      approvalId: '12345678-1234-4234-8234-123456789012',
      expectedPlaybookDigest,
    };
    const globals = {
      ...keys,
      ...approval,
      ...playbooks,
      defineEventHandler: (handler) => handler,
      readBody: async () => body,
      createError: (value) =>
        Object.assign(new Error(value.statusMessage), value),
      useRuntimeConfig: () => ({
        executionQueueUrl: 'fixture-queue',
        dynamodbTableName: 'fixture-table',
        s3ReportBucket: 'fixture-bucket',
      }),
      useDynamoDB: () => ({
        send: async (command) => {
          events.push({ type: command.constructor.name, input: command.input });
          if (command.constructor.name === 'QueryCommand')
            return {
              Items: [
                {
                  PK: 'RCA#fixture',
                  SK: 'headless-codex#SESSION',
                  engine: 'headless-codex',
                  state: 'COMPLETED',
                  confirmed: true,
                  report_s3_key: 'fixture-report.md',
                  playbook_id: 'fixed-book',
                  playbook: currentBook,
                },
              ],
            };
          return {};
        },
      }),
      useS3: () => ({
        send: async (command) => {
          events.push({ type: command.constructor.name, input: command.input });
          if (
            command.constructor.name === 'HeadObjectCommand' &&
            command.input.Key.startsWith('approvals/')
          ) {
            throw Object.assign(new Error('missing'), {
              name: 'NotFound',
              $metadata: { httpStatusCode: 404 },
            });
          }
          return {};
        },
      }),
      useSqs: () => ({
        send: async (command) => {
          events.push({ type: command.constructor.name, input: command.input });
          return {};
        },
      }),
    };
    const module = { exports: {} };
    new Function('require', 'module', 'exports', ...Object.keys(globals), code)(
      dashboardRequire,
      module,
      module.exports,
      ...Object.values(globals),
    );
    try {
      return { response: await module.exports.default({}), events };
    } catch (error) {
      return { error, events };
    }
  }
  for (const value of [undefined, '', 'invalid', 123]) {
    const result = await run(value);
    assert.equal(result.error?.statusCode, 400);
    assert.deepEqual(
      result.events,
      [],
      'a missing inspected version causes no AWS calls',
    );
  }
  const changed = structuredClone(book);
  changed.execution_steps[0].commands[0] =
    changed.execution_steps[0].commands[0].replace(
      '--task owner',
      '--task other',
    );
  const stale = await run(expected, changed);
  assert.equal(stale.error?.statusCode, 409);
  assert.ok(
    stale.events.every((event) =>
      ['QueryCommand', 'HeadObjectCommand'].includes(event.type),
    ),
  );
  assert.ok(
    stale.events.every((event) => !event.input.Key?.startsWith('approvals/')),
    'stale approval never touches its snapshot',
  );

  const accepted = await run(expected);
  assert.equal(accepted.error, undefined);
  const kinds = accepted.events.map((event) => event.type);
  assert.ok(
    kinds.indexOf('PutObjectCommand') < kinds.indexOf('TransactWriteCommand'),
  );
  assert.ok(
    kinds.indexOf('TransactWriteCommand') < kinds.indexOf('SendMessageCommand'),
  );
  const stored = accepted.events.find(
    (event) => event.type === 'PutObjectCommand',
  );
  assert.deepEqual(
    stored.input.Body,
    approval.serializePlaybookSnapshot(book),
    'the exact inspected command bytes are stored',
  );
  const published = JSON.parse(
    accepted.events.find((event) => event.type === 'SendMessageCommand').input
      .MessageBody,
  );
  assert.equal(published.playbook_digest, expected);
});

test('an approval carries a stable identifier so a resubmit cannot double-execute', async () => {
  const [source, page] = await Promise.all([
    readRepositoryFile('packages/dashboard/server/api/executions.post.ts'),
    readRepositoryFile('packages/dashboard/app/pages/report/[id].vue'),
  ]);

  assert.match(source, /const executionId = approvalId/);
  assert.match(source, /execution_id: executionId/);
  assert.match(source, /approval_id: approvalId/);
  assert.match(source, /rca_id: rcaId/);
  assert.match(source, /engine,/);
  assert.match(source, /requested_by: APPROVAL_REQUESTED_BY/);
  assert.doesNotMatch(source, /requestedBy/);

  // A failed publish retries the same reservation and snapshot.
  assert.match(page, /pendingApprovalId\.value \?\?= crypto\.randomUUID\(\)/);
  assert.match(page, /approvalId: pendingApprovalId\.value/);
  const submit = page.slice(page.indexOf('async function approveExecution()'));
  assert.ok(
    submit.indexOf('pendingApprovalId.value = null') >
      submit.indexOf("await $fetch('/api/executions'"),
    'the client clears the UUID only after the approval request succeeds',
  );
});

test('a retrospective revision becomes the procedure the next execution runs', async () => {
  const { resolveCurrentPlaybook } =
    await importRepositoryModule(PLAYBOOK_MODULE);
  const session = {
    SK: 'strands#SESSION',
    state: 'COMPLETED',
    playbook_id: 'current',
    playbook_span_id: 'selected-span',
  };
  const selectedSpan = {
    SK: 'strands#SPAN#selected-span',
    engine: 'strands',
    span_type: 'PLAYBOOK',
    metadata: { playbook_id: 'current', execution_steps: [] },
  };
  const wrongFirstSpan = {
    SK: 'strands#SPAN#wrong-first',
    engine: 'strands',
    span_type: 'PLAYBOOK',
    metadata: { playbook_id: 'other', execution_steps: [{ step_id: 'bad' }] },
  };
  const wrongRevision = {
    SK: 'strands#PLAYBOOK_REVISION',
    playbook_id: 'other',
    playbook: JSON.stringify({
      playbook_id: 'other',
      execution_steps: [{ step_id: 'wrong-revision' }],
    }),
  };

  assert.equal(
    resolveCurrentPlaybook(
      [wrongFirstSpan, wrongRevision, selectedSpan],
      session,
      'strands',
    )?.sourceItem.SK,
    selectedSpan.SK,
    'a revision for another playbook and an arbitrary first span are ignored',
  );

  const matchingRevision = {
    ...wrongRevision,
    playbook_id: 'current',
    playbook: JSON.stringify({
      playbook_id: 'current',
      execution_steps: [{ step_id: 'revised' }],
    }),
  };
  assert.equal(
    resolveCurrentPlaybook([selectedSpan, matchingRevision], session, 'strands')
      ?.source,
    'revision',
  );
});

test('playbook selection is exact for CC sessions and conservative for legacy Strands sessions', async () => {
  const { resolveCurrentPlaybook } =
    await importRepositoryModule(PLAYBOOK_MODULE);
  const ccPlaybook = {
    playbook_id: 'cc-current',
    execution_steps: [{ step_id: 'persisted' }],
  };
  for (const engine of ['headless-codex', 'codex-headless', 'cc-headless']) {
    const arbitrarySpan = {
      SK: `${engine}#SPAN#other`,
      engine,
      span_type: 'PLAYBOOK',
      metadata: {
        playbook_id: 'cc-current',
        execution_steps: [{ step_id: 'span-copy' }],
      },
    };
    const cc = resolveCurrentPlaybook(
      [arbitrarySpan],
      {
        SK: `${engine}#SESSION`,
        state: 'COMPLETED',
        playbook_id: 'cc-current',
        playbook: JSON.stringify(ccPlaybook),
      },
      engine,
    );
    assert.deepEqual(cc?.playbook, ccPlaybook);
    assert.equal(cc?.source, 'session');
  }

  const legacySession = {
    SK: 'SESSION',
    state: 'COMPLETED',
    playbook_id: 'legacy',
  };
  const legacySpan = (id) => ({
    SK: `SPAN#${id}`,
    span_type: 'PLAYBOOK',
    metadata: { playbook_id: 'legacy', execution_steps: [] },
  });
  assert.equal(
    resolveCurrentPlaybook(
      [legacySpan('one'), legacySpan('two')],
      legacySession,
      'strands',
    ),
    null,
    'legacy fallback rejects an ambiguous set of PLAYBOOK spans',
  );
  assert.equal(
    resolveCurrentPlaybook([legacySpan('only')], legacySession, 'strands')
      ?.source,
    'legacy-span',
  );
  assert.equal(
    resolveCurrentPlaybook(
      [legacySpan('only')],
      { ...legacySession, state: 'REPORT_GENERATION' },
      'strands',
    ),
    null,
    'an intermediate PLAYBOOK span is not a completed report artifact',
  );
});

test('executable playbooks require complete uniquely identified steps', async () => {
  const { countExecutionSteps, validateExecutablePlaybook } =
    await importRepositoryModule(PLAYBOOK_MODULE);
  const validStep = {
    step_id: 'restart-service',
    action: 'Restart the affected service',
    success_criteria: 'Healthy task count returns to target',
    commands: [
      'aws ecs update-service --cluster demo --service healthcare --force-new-deployment --region us-east-1',
    ],
  };

  assert.equal(
    validateExecutablePlaybook({ execution_steps: [validStep] }).valid,
    true,
  );
  for (const steps of [
    [],
    [null],
    [{ ...validStep, action: ' ' }],
    [{ ...validStep, success_criteria: '' }],
    [validStep, { ...validStep }],
  ]) {
    assert.equal(
      validateExecutablePlaybook({ execution_steps: steps }).valid,
      false,
      `invalid steps must be rejected: ${JSON.stringify(steps)}`,
    );
  }

  const items = [
    {
      SK: 'headless-codex#SESSION',
      confirmed: false,
      playbook_id: 'pb-1',
      playbook: JSON.stringify({
        playbook_id: 'pb-1',
        execution_steps: [validStep],
      }),
    },
  ];
  assert.equal(
    countExecutionSteps(items, 'headless-codex'),
    0,
    'readiness cannot offer an unconfirmed procedure that approval rejects',
  );
});

test('runbooks reject prose-only history and preserve it for read-only inspection', async () => {
  const { validateExecutablePlaybook, readableExecutionSteps } =
    await importRepositoryModule(PLAYBOOK_MODULE);
  const legacy = {
    execution_steps: [
      {
        step_id: 'legacy',
        action: 'Restart service',
        success_criteria: 'healthy',
      },
    ],
  };
  assert.equal(validateExecutablePlaybook(legacy).valid, false);
  assert.equal(readableExecutionSteps(legacy)[0].action, 'Restart service');
  assert.deepEqual(readableExecutionSteps(legacy)[0].commands, []);
});

test('runbooks require fixed command coordinates and bounded approved metric waits', async () => {
  const { validateExecutablePlaybook } =
    await importRepositoryModule(PLAYBOOK_MODULE);
  const action = {
    step_id: 'stop',
    action: 'Stop confirmed owner',
    success_criteria: 'stop acknowledged',
    commands: [
      'aws cloudwatch list-metrics --namespace Healthcare/Sensor --region us-east-1',
      'aws cloudwatch describe-alarms --alarm-names IngestFailures --region us-east-1',
      'aws ecs stop-task --cluster demo --task arn:aws:ecs:us-east-1:123456789012:task/demo/owner --region us-east-1',
    ],
  };
  const metric = (name) => ({
    namespace: 'Healthcare/Sensor',
    metric_name: name,
    dimensions: { ServiceName: 'healthcare' },
  });
  const wait = {
    step_id: 'verify',
    action: 'Observe recovery',
    success_criteria: 'Failures zero with traffic and IngestFailures OK',
    metric_wait: {
      action_step_id: 'stop',
      region: 'us-east-1',
      failure_alarm_name: 'IngestFailures',
      max_wait_seconds: 300,
      metrics: { attempts: metric('Attempts'), failures: metric('Failures') },
    },
  };
  assert.equal(
    validateExecutablePlaybook({ execution_steps: [action, wait] }).valid,
    true,
  );
  for (const commands of [
    [],
    ['aws ecs stop-task --task <target> --region us-east-1'],
    ['aws ecs stop-task --task owner'],
    ['aws ecs stop-task --task ${TARGET} --region us-east-1'],
  ]) {
    assert.equal(
      validateExecutablePlaybook({ execution_steps: [{ ...action, commands }] })
        .valid,
      false,
    );
  }
  for (const metric_wait of [
    { ...wait.metric_wait, max_wait_seconds: 301 },
    { ...wait.metric_wait, action_step_id: 'missing' },
    { ...wait.metric_wait, region: '' },
    { ...wait.metric_wait, metrics: { attempts: metric('Attempts') } },
  ]) {
    assert.equal(
      validateExecutablePlaybook({
        execution_steps: [action, { ...wait, metric_wait }],
      }).valid,
      false,
    );
  }
  assert.equal(
    validateExecutablePlaybook({ execution_steps: [wait, action] }).valid,
    false,
  );
  assert.equal(
    validateExecutablePlaybook({
      execution_steps: [action, { ...wait, commands: action.commands }],
    }).valid,
    false,
  );
});

test('shared runbook fixtures reject ambiguous approvals and preserve exact command identity', async (t) => {
  const { validateExecutablePlaybook, readableExecutionSteps } =
    await importRepositoryModule(PLAYBOOK_MODULE);
  const { serializePlaybookSnapshot, sha256Hex } =
    await importRepositoryModule(APPROVAL_MODULE);
  const { cases } = JSON.parse(
    await readRepositoryFile('tests/fixtures/runbook-contract.json'),
  );
  for (const fixture of cases) {
    await t.test(fixture.name, () => {
      const playbook = { execution_steps: fixture.steps };
      const before = JSON.stringify(playbook);
      const digest = sha256Hex(serializePlaybookSnapshot(playbook));
      const result = validateExecutablePlaybook(playbook);
      assert.equal(result.valid, fixture.valid, result.reason);
      assert.equal(
        JSON.stringify(playbook),
        before,
        'validation must not rewrite approval inputs',
      );
      assert.equal(sha256Hex(serializePlaybookSnapshot(playbook)), digest);
      if (!fixture.valid) {
        assert.deepEqual(
          result.steps,
          [],
          'invalid input never contributes executable readiness',
        );
        assert.ok(result.reason);
      } else {
        assert.deepEqual(result.steps, fixture.steps);
        const displayed = readableExecutionSteps(playbook);
        for (let i = 0; i < fixture.steps.length; i++) {
          assert.deepEqual(
            displayed[i].commands,
            fixture.steps[i].commands ?? [],
          );
          assert.deepEqual(
            displayed[i].metric_wait,
            fixture.steps[i].metric_wait ?? null,
          );
        }
      }
    });
  }
});

test('approval snapshots are deterministic and reservation retries require an exact match', async () => {
  const { executionReservationMatches, serializePlaybookSnapshot, sha256Hex } =
    await importRepositoryModule(APPROVAL_MODULE);
  const first = serializePlaybookSnapshot({
    z: 1,
    nested: { b: true, a: '값' },
  });
  const second = serializePlaybookSnapshot({
    nested: { a: '값', b: true },
    z: 1,
  });
  assert.deepEqual(first, second);
  assert.equal(sha256Hex(first), sha256Hex(second));

  const request = {
    execution_id: '11111111-1111-4111-8111-111111111111',
    rca_id: 'rca-1',
    engine: 'strands',
    approval_id: '11111111-1111-4111-8111-111111111111',
    requested_by: 'dashboard',
    report_s3_key: 'reports/strands/rca-1.md',
    approved_playbook_s3_key:
      'approvals/rca-1/11111111-1111-4111-8111-111111111111/playbook.json',
    playbook_digest: sha256Hex(first),
  };
  assert.equal(
    executionReservationMatches(
      { ...request, execution_state: 'PENDING_APPROVAL', attempt: 0 },
      request,
    ),
    true,
  );
  assert.equal(
    executionReservationMatches(
      {
        ...request,
        execution_state: 'PENDING_APPROVAL',
        attempt: 0,
        playbook_digest: '0'.repeat(64),
      },
      request,
    ),
    false,
  );
});

test('approval persistence precedes queue publication and carries the full worker contract', async () => {
  const source = await readRepositoryFile(
    'packages/dashboard/server/api/executions.post.ts',
  );
  assert.match(source, /PutObjectCommand/);
  assert.match(source, /IfNoneMatch: '\*'/);
  assert.match(source, /TransactWriteCommand/);
  assert.match(source, /execution_state: 'PENDING_APPROVAL'/);
  assert.match(source, /SK: ACTIVE_EXECUTION_SK/);
  assert.match(source, /attempt: 0/);
  assert.match(source, /ttl,/);
  assert.match(source, /executionReservationMatches/);
  assert.ok(
    source.indexOf('await reserveExecution') <
      source.indexOf('new SendMessageCommand'),
    'the active reservation is authoritative before queue publication',
  );
  for (const field of [
    'execution_id',
    'rca_id',
    'engine',
    'approval_id',
    'requested_by',
    'report_s3_key',
    'approved_playbook_s3_key',
    'playbook_digest',
  ]) {
    assert.ok(source.includes(field), `approval includes ${field}`);
  }
});

test('a person deciding to approve can tell a proven procedure from a draft', async () => {
  const [playbookApi, reportPage] = await Promise.all([
    readRepositoryFile('packages/dashboard/server/api/playbooks/[id].get.ts'),
    readRepositoryFile('packages/dashboard/app/pages/report/[id].vue'),
  ]);

  // The promotion is recorded on the revision, so reading the status off the
  // analysis span alone would show a draft forever. The API reads it from the
  // exact object returned by the shared revision-aware resolver.
  assert.match(playbookApi, /resolveCurrentPlaybook/);
  assert.match(
    playbookApi,
    /verification_status: readText\(playbook\.verification_status\)/,
    'the playbook API must read the status from the resolved current playbook',
  );

  // Anything other than the recorded VERIFIED has to read as a draft: an
  // unproven procedure looking proven is what misleads the approver.
  assert.match(reportPage, /verification_status === 'VERIFIED'/);
  assert.match(
    reportPage,
    /플레이북 검증됨/,
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

test('execution history is scoped to the report engine', async () => {
  const [historyApi, reportPage, sessionApi] = await Promise.all([
    readRepositoryFile(
      'packages/dashboard/server/api/executions/[rcaId].get.ts',
    ),
    readRepositoryFile('packages/dashboard/app/pages/report/[id].vue'),
    readRepositoryFile(
      'packages/dashboard/server/api/sessions/[id]/index.get.ts',
    ),
  ]);
  assert.match(historyApi, /isAllowedEngine\(engine\)/);
  assert.match(
    historyApi,
    /execution\.engine === engine/,
    'history excludes attempts belonging to the other analysis engine',
  );
  assert.match(reportPage, /query: \{ engine \}/);
  assert.match(
    sessionApi,
    /execution\.engine === engine/,
    'the report summary cannot be labelled by the other engine execution',
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
  //
  // The operations theme has semantic error/success colors and visible labels.
  // Execute the mapping so unrelated class strings cannot satisfy the assertion.
  const reportTone = reportPage.match(
    /function executionTone\(state: string\): string \{[\s\S]*?\n\}/,
  );
  assert.ok(reportTone, 'report page maps execution state to a tone');
  const tone = new Function(
    `return (${reportTone[0].replace('(state: string): string', '(state)')});`,
  )();
  assert.equal(tone('UNRESOLVED'), 'text-error');
  assert.equal(tone('FAILED'), 'text-error');
  assert.equal(tone('RESOLVED'), 'text-success');
  assert.match(
    reportPage,
    /\{\{\s*execution\.stateLabel\s*\}\}/,
    'color is accompanied by the recorded outcome label',
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
  assert.doesNotMatch(
    OUTCOME_TONE[resolved],
    /mark-broken/,
    'a resolved incident is not marked as broken',
  );

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
    assert.match(
      OUTCOME_TONE[outcome],
      /mark-broken/,
      `${failed} execution must be marked as broken, not left to read as success`,
    );
    assert.notEqual(
      OUTCOME_TONE[outcome],
      OUTCOME_TONE[resolved],
      `${failed} execution must not be toned the same as a resolved one`,
    );
  }

  // Whatever marks a break must survive without colour, since a reader who
  // cannot resolve the rule has only the word. Every outcome therefore carries a
  // label, while active work keeps the semantic colours defined by DESIGN.md.
  const { OUTCOME_LABEL } = await import(
    pathToFileURL(
      path.join(
        REPOSITORY_ROOT,
        'packages/dashboard/app/utils/sessionState.ts',
      ),
    ).href
  );
  for (const key of Object.keys(OUTCOME_TONE)) {
    assert.ok(
      OUTCOME_LABEL[key],
      `${key} needs a word, because its tone alone cannot state it`,
    );
  }
  assert.match(OUTCOME_TONE.RUNNING, /text-info/);
  assert.match(OUTCOME_TONE.AWAITING, /text-warning/);
  for (const key of [
    'RESOLVED',
    'UNRESOLVED',
    'NO_CAUSE',
    'BROKEN',
    'SKIPPED',
  ]) {
    assert.doesNotMatch(
      OUTCOME_TONE[key],
      /text-info/,
      `${key} is not analysis in progress, so it must not borrow that tone`,
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
  assert.match(source, /<RecoveryPlanSteps[\s\S]*?:steps="executionSteps"/);
  assert.match(source, /:executable="playbook\.executable === true"/);
  assert.match(source, /verification_status/);

  // Approval requires a completed analysis, steps to run, and nothing already
  // running — the same conditions the API enforces. The state check is asserted
  // on the optional-chained form the gate itself uses, so this does not pass on
  // an unrelated mention of the same state elsewhere in the page.
  assert.match(source, /session\.value\?\.state === 'COMPLETED'/);
  assert.match(source, /session\.value\?\.confirmed === true/);
  assert.match(source, /executionSteps\.value\.length > 0/);
  assert.match(source, /playbook\.value\?\.executable === true/);
  assert.match(source, /hasFixedDefinitions\.value &&/);
  assert.match(source, /!inFlight\.value/);
  assert.match(source, /:disabled="!canApprove"/);

  // Writing starts only after an explicit confirmation.
  assert.match(source, /@click="openApproval\(\)"/);
  assert.match(source, /approvalModal\.value\?\.showModal\(\)/);
  assert.match(source, /:disabled="approving \|\| !canApprove"/);
  assert.match(
    source,
    /async function approveExecution\(\) \{\s*if \(!canApprove\.value\)/,
  );
});

test('report execution polling is visible, nonoverlapping, terminal-aware and leaves the reviewed playbook untouched', async () => {
  const page = await readRepositoryFile(
    'packages/dashboard/app/pages/report/[id].vue',
  );
  const source = page.slice(
    page.indexOf('let executionPollTimer:'),
    page.indexOf('useHead('),
  );
  assert.doesNotMatch(source, /refreshPlaybook/);
  const dashboardRequire = createRequire(
    path.join(REPOSITORY_ROOT, 'packages/dashboard/package.json'),
  );
  const { transpileModule, ScriptTarget } = dashboardRequire('typescript');
  const code = transpileModule(source, {
    compilerOptions: { target: ScriptTarget.ES2022 },
  }).outputText;
  let mounted, unmounted, tick, visible, cleared, removed;
  const inFlight = { value: true };
  let historyReads = 0;
  let sessionReads = 0;
  let release;
  const pending = new Promise((resolve) => {
    release = resolve;
  });
  const document = {
    visibilityState: 'visible',
    addEventListener: (_event, callback) => {
      visible = callback;
    },
    removeEventListener: (_event, callback) => {
      removed = callback;
    },
  };
  const window = {
    setInterval: (callback, delay) => {
      tick = callback;
      assert.equal(delay, 5000);
      return 17;
    },
    clearInterval: (id) => {
      cleared = id;
    },
  };
  new Function(
    'document',
    'window',
    'inFlight',
    'refreshExecutions',
    'refreshSession',
    'onMounted',
    'onBeforeUnmount',
    code,
  )(
    document,
    window,
    inFlight,
    async () => {
      historyReads++;
      await pending;
    },
    async () => {
      sessionReads++;
    },
    (callback) => {
      mounted = callback;
    },
    (callback) => {
      unmounted = callback;
    },
  );
  mounted();
  const first = tick();
  await tick();
  assert.equal(historyReads, 1, 'a slow request is not duplicated');
  release();
  await first;
  assert.equal(sessionReads, 1);
  inFlight.value = false;
  await tick();
  assert.equal(historyReads, 1, 'terminal execution stops polling');
  inFlight.value = true;
  document.visibilityState = 'hidden';
  await tick();
  assert.equal(historyReads, 1, 'hidden pages do not poll');
  document.visibilityState = 'visible';
  await visible();
  assert.equal(historyReads, 2);
  assert.equal(sessionReads, 2);
  unmounted();
  assert.equal(cleared, 17);
  assert.equal(removed, visible);
});

test('approval binds the inspected digest and retains its UUID only for the same snapshot retry', async () => {
  const page = await readRepositoryFile(
    'packages/dashboard/app/pages/report/[id].vue',
  );
  const api = await readRepositoryFile(
    'packages/dashboard/server/api/playbooks/[id].get.ts',
  );
  assert.match(
    api,
    /playbookDigest: sha256Hex\(serializePlaybookSnapshot\(playbook\)\)/,
  );
  const dashboardRequire = createRequire(
    path.join(REPOSITORY_ROOT, 'packages/dashboard/package.json'),
  );
  const nuxtRequire = createRequire(
    dashboardRequire.resolve('nuxt/package.json'),
  );
  const { ref, computed } = nuxtRequire('vue');
  const { transpileModule, ScriptTarget } = dashboardRequire('typescript');
  const source = page.slice(
    page.indexOf('const executions ='),
    page.indexOf('function executionTone('),
  );
  assert.ok(source.includes('async function approveExecution()'));
  const code = transpileModule(source, {
    compilerOptions: { target: ScriptTarget.ES2022 },
  }).outputText;
  const session = ref({
    engine: 'headless-codex',
    state: 'COMPLETED',
    confirmed: true,
  });
  const playbook = ref({
    executable: true,
    playbookDigest: 'a'.repeat(64),
    execution_steps: [
      {
        step_id: 'stop',
        action: 'stop owner',
        success_criteria: 'stopped',
        commands: [
          'aws ecs stop-task --cluster demo --task owner --region us-east-1',
        ],
      },
    ],
  });
  const history = ref({ executions: [] });
  const requests = [];
  let fail = true;
  const submit = async (_path, request) => {
    requests.push(structuredClone(request.body));
    if (fail) throw { data: { statusMessage: 'retry the identical request' } };
  };
  const controller = new Function(
    'computed',
    'ref',
    'session',
    'playbook',
    'executionHistory',
    '$fetch',
    'refreshExecutions',
    'refreshPlaybook',
    'id',
    `${code}\nreturn { canApprove, openApproval, approveExecution, reloadPlanForReview, approvalModal, approvalError };`,
  )(
    computed,
    ref,
    session,
    playbook,
    history,
    submit,
    async () => {},
    async () => {},
    'rca-fixture',
  );
  controller.approvalModal.value = { showModal() {}, close() {} };
  assert.equal(controller.canApprove.value, true);
  controller.openApproval();
  playbook.value.playbookDigest = 'b'.repeat(64);
  await controller.approveExecution();
  assert.equal(
    requests.length,
    0,
    'a changed displayed snapshot cannot be silently approved',
  );
  assert.match(controller.approvalError.value, /변경/);
  controller.openApproval();
  await controller.approveExecution();
  await controller.approveExecution();
  assert.equal(requests.length, 2);
  assert.equal(
    requests[0].approvalId,
    requests[1].approvalId,
    'transport retries keep one reservation',
  );
  assert.equal(requests[0].expectedPlaybookDigest, 'b'.repeat(64));
  await controller.reloadPlanForReview();
  playbook.value.playbookDigest = 'c'.repeat(64);
  controller.openApproval();
  fail = false;
  await controller.approveExecution();
  assert.notEqual(
    requests[2].approvalId,
    requests[0].approvalId,
    'a newly reviewed version starts a new approval',
  );
  assert.equal(requests[2].expectedPlaybookDigest, 'c'.repeat(64));
  playbook.value.executable = false;
  assert.equal(controller.canApprove.value, false);
  playbook.value.executable = true;
  playbook.value.execution_steps[0].commands = [];
  assert.equal(
    controller.canApprove.value,
    false,
    'legacy prose stays read-only even with an old server',
  );
});

test('legacy recovery plans retain their prose and criteria without pretending commands exist', async () => {
  const html = await renderRecoveryPlan({
    steps: [
      {
        step_id: 'legacy-1',
        action: '이전 작업 원문 <script>alert(1)</script>',
        intent: '연결된 세션 확인',
        success_criteria: '실패 지표가 0인지 확인',
      },
    ],
    executable: false,
    validationError: '명령이 없는 과거 계획입니다',
  });
  for (const text of [
    '이전 작업 원문',
    '연결된 세션 확인',
    '실패 지표가 0인지 확인',
    '성공 판정 기준',
    '명령 미생성 · 새 분석 필요',
  ]) {
    assert.ok(html.includes(text), `legacy display preserves ${text}`);
  }
  assert.doesNotMatch(html, /사전 확정 런북|<script>|<pre/);
  assert.match(html, /&lt;script&gt;/, 'model text is escaped');
  assert.doesNotMatch(
    html,
    /원인 미확정|근본원인이 확정되지 않아/,
    'missing commands do not imply an unconfirmed cause',
  );
});

test('fixed runbooks render ordered commands and the exact post-action observation coordinates', async () => {
  const commands = [
    'aws ecs describe-tasks --region us-east-1 --cluster demo --tasks task-1',
    'aws ecs stop-task --region us-east-1 --cluster demo --task task-1',
  ];
  const metric_wait = {
    action_step_id: 'action-1',
    region: 'us-east-1',
    failure_alarm_name: 'demo-failure',
    max_wait_seconds: 240,
    metrics: {
      attempts: {
        namespace: 'Demo/Sensor',
        metric_name: 'Attempts',
        dimensions: { ServiceName: 'demo-service' },
      },
      failures: {
        namespace: 'Demo/Sensor',
        metric_name: 'Failures',
        dimensions: { ServiceName: 'demo-service' },
      },
    },
  };
  const html = await renderRecoveryPlan({
    steps: [
      {
        step_id: 'action-1',
        action: '대상 작업 중단',
        success_criteria: '대상 작업 중단 확인',
        commands,
      },
      {
        step_id: 'observe-1',
        action: '조치 후 검증',
        success_criteria: '고정 구간 실패 없음',
        metric_wait,
      },
    ],
    executable: true,
  });
  assert.match(html, /사전 확정 런북 · 승인 전 검토/);
  assert.ok(html.includes(commands[0]) && html.includes(commands[1]));
  assert.ok(
    html.indexOf(commands[0]) < html.indexOf(commands[1]),
    'command order remains unchanged',
  );
  assert.equal(
    (html.match(/<pre /g) || []).length,
    3,
    'two command blocks plus the full observation JSON',
  );
  for (const value of [
    '조치 후 고정 구간 관측',
    'action-1',
    'us-east-1',
    'demo-failure',
    '240초',
    'Demo/Sensor',
    'Attempts',
    'Failures',
    'ServiceName',
    'demo-service',
    '고정 관측 설정 JSON 전체',
  ]) {
    assert.ok(html.includes(value), `observation display includes ${value}`);
  }
  assert.doesNotMatch(html, /명령 미생성/);
});

test('a partially defined runbook remains visibly unapprovable without hiding either kind of step', async () => {
  const html = await renderRecoveryPlan({
    steps: [
      {
        step_id: 'old',
        action: '기존 자연어 계획',
        success_criteria: '기존 기준',
      },
      {
        step_id: 'fixed',
        action: '알람 조회',
        success_criteria: '알람 확인',
        commands: [
          'aws cloudwatch describe-alarms --region us-east-1 --alarm-names demo',
        ],
      },
    ],
    executable: false,
    validationError: 'old 단계에 고정 실행 정의가 없습니다',
  });
  assert.match(html, /실행 정의 확인 필요 · 승인 불가/);
  assert.match(html, /기존 자연어 계획/);
  assert.match(html, /aws cloudwatch describe-alarms/);
  assert.match(html, /old 단계에 고정 실행 정의가 없습니다/);
  assert.doesNotMatch(html, /사전 확정 런북/);
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
    'packages/headless-codex/src/headless_codex/adapters/secondary/session/dynamodb_session_store.py',
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

test('dashboard state graph uses one lifecycle for both analysis engines', async () => {
  const [graphSource, tracePageSource] = await Promise.all([
    readRepositoryFile('packages/dashboard/app/components/StateGraph.vue'),
    readRepositoryFile('packages/dashboard/app/pages/trace/[id].vue'),
  ]);

  assert.match(graphSource, /ANALYSIS_HAPPY_PATH = \[/);
  assert.match(graphSource, /HYPOTHESIS_PRIORITIZATION/);
  assert.match(graphSource, /EVIDENCE_COLLECTION/);
  assert.match(graphSource, /LEGACY_HEADLESS_HAPPY_PATH/);
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
    /const engines = \['strands', 'headless-codex'\]/,
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
    'ANALYSIS#SESSION',
    'strands#SESSION',
    'SESSION',
  ]);
  assert.deepEqual(sessionSkCandidates('headless-codex'), [
    'ANALYSIS#SESSION',
    'headless-codex#SESSION',
  ]);
  assert.deepEqual(sessionSkCandidates('codex-headless'), [
    'ANALYSIS#SESSION',
    'codex-headless#SESSION',
  ]);
  assert.deepEqual(sessionSkCandidates('cc-headless'), [
    'ANALYSIS#SESSION',
    'cc-headless#SESSION',
  ]);
  assert.equal(
    hypothesisSkPrefix('strands', 'ANALYSIS#SESSION'),
    'strands#HYPO#',
  );

  // Only hypotheses belonging to the resolved session representation are closed.
  assert.equal(hypothesisSkPrefix('strands', 'SESSION'), 'HYPO#');
  assert.equal(
    hypothesisSkPrefix('strands', 'strands#SESSION'),
    'strands#HYPO#',
  );
  assert.equal(
    hypothesisSkPrefix('headless-codex', 'headless-codex#SESSION'),
    'headless-codex#HYPO#',
  );
  assert.equal(
    hypothesisSkPrefix('codex-headless', 'codex-headless#SESSION'),
    'codex-headless#HYPO#',
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

test('deleting a session removes its artifacts without stripping the other engine', async () => {
  const source = await readRepositoryFile(
    'packages/dashboard/server/api/sessions/[id].delete.ts',
  );

  // Artifacts live under a path per RCA, per engine and per attempt, so a
  // session's reports are a prefix rather than one key. Deleting a single
  // `reports/{id}.md` left everything behind, and the objects then sat until the
  // lifecycle rule swept them weeks later — a deleted session kept its evidence
  // readable the whole time.
  assert.match(
    source,
    /ListObjectsV2Command/,
    'the delete enumerates a prefix',
  );
  assert.match(source, /DeleteObjectsCommand/, 'and removes what it found');
  assert.doesNotMatch(
    source,
    /`reports\/\$\{id\}\.md`/,
    'the single-key delete cannot reach the artifacts the engines write',
  );

  // Reports are engine-scoped, so one engine's deletion takes only its own.
  assert.match(source, /`reports\/\$\{engine\}\/\$\{id\}\//);

  // Evidence is not engine-scoped: both engines analyse the same alarm under one
  // RCA id, so removing it while the other engine's session survives would strip
  // the evidence that session's report cites.
  assert.match(source, /survivingEngines/);
  assert.match(
    source,
    /if \(!survivingEngines\.size\) prefixes\.push\(`rca\/\$\{id\}\/`\)/,
    'shared evidence goes only when no session for this RCA is left',
  );
});
