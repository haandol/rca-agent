import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { createServer } from 'node:http';
import path from 'node:path';
import test from 'node:test';
import { readPublicationHistory } from '../../packages/dashboard/server/utils/retrospectivePublication.ts';
import {
  readExecution,
  isExecutionItem,
} from '../../packages/dashboard/server/utils/execution.ts';
import * as keys from '../../packages/dashboard/server/utils/keys.ts';
import { shouldPollPublication } from '../../packages/dashboard/app/utils/publication.ts';

const root = path.resolve(import.meta.dirname, '../..');
const require = createRequire(
  path.join(root, 'packages/dashboard/package.json'),
);
const nuxtRequire = createRequire(require.resolve('nuxt/package.json'));
const ts = require('typescript');
const now = Math.floor(Date.now() / 1000);
const eid = 'execution-a';
const digest = 'a'.repeat(64);
const review = 'b'.repeat(64);
const revision = 'd'.repeat(64);
const execution = {
  PK: 'RCA#incident-a',
  SK: `EXEC#${eid}`,
  rca_id: 'incident-a',
  execution_id: eid,
  engine: 'strands',
  approval_id: eid,
  playbook_digest: digest,
  execution_state: 'RESOLVED',
  source_part: 'recovery',
  source_part_revision: revision,
  source_part_payload_sha256: revision,
  retrospective_status: 'FAILED',
  retrospective_summary: 'Original failure retained',
  ttl: now + 600,
};
function child(overrides = {}) {
  return {
    PK: execution.PK,
    SK: `RETROSPECTIVE_FOLLOWUP#${eid}#${review}`,
    record_type: 'RETROSPECTIVE_FOLLOWUP',
    schema_version: 1,
    rca_id: execution.rca_id,
    execution_id: eid,
    engine: 'strands',
    approval_id: eid,
    playbook_digest: digest,
    review_status: 'COMPLETED',
    status: 'WAITING_FOR_PUBLICATION',
    reason: 'Canonical source pending',
    attempt_id: review,
    review_sha256: review,
    source_part_revision: revision,
    public_playbook_id: 'public-book',
    published_revision: 'revision-1',
    created_at: now - 20,
    updated_at: now - 10,
    ttl: now + 300,
    ...overrides,
  };
}
function transpile(source) {
  return ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
}
function handler(file, globals) {
  const module = { exports: {} };
  new Function(
    'require',
    'module',
    'exports',
    ...Object.keys(globals),
    transpile(readFileSync(path.join(root, file), 'utf8')),
  )(require, module, module.exports, ...Object.values(globals));
  return module.exports.default;
}
async function httpGet(run) {
  const server = createServer(async (req, res) => {
    try {
      res.setHeader('content-type', 'application/json');
      res.end(JSON.stringify(await run(req)));
    } catch (error) {
      res.statusCode = error.statusCode || 500;
      res.end(JSON.stringify({ error: error.message }));
    }
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  try {
    const response = await fetch(
      `http://127.0.0.1:${server.address().port}/?engine=strands`,
    );
    assert.equal(response.status, 200);
    return await response.json();
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}
function globals(ddb) {
  return {
    ...keys,
    defineEventHandler: (fn) => fn,
    getRouterParam: (_, name) => (name === 'executionId' ? eid : 'incident-a'),
    getQuery: () => ({ engine: 'strands' }),
    useRuntimeConfig: () => ({
      dynamodbTableName: 'mock-table',
      s3ReportBucket: 'mock-bucket',
    }),
    useDynamoDB: () => ddb,
    useS3: () => ({
      send: () => {
        throw Error('Unexpected S3 call');
      },
    }),
    createError: (value) => Object.assign(Error(value.statusMessage), value),
    readExecution,
    isExecutionItem,
    readPublicationHistory,
    hasAnalysisParts: () => true,
  };
}

test('HTTP execution history preserves old FAILED and RESOLVED beside a live new follow-up', async () => {
  const items = [execution, child()];
  const route = handler(
    'packages/dashboard/server/api/executions/[rcaId].get.ts',
    {
      ...globals({
        send: () => {
          throw Error('Unexpected write');
        },
      }),
      readAnalysisPartition: async () => items,
    },
  );
  const result = await httpGet(route);
  assert.equal(result.executions[0].state, 'RESOLVED');
  assert.equal(result.executions[0].retrospectiveStatus, 'FAILED');
  assert.equal(
    result.executions[0].retrospectiveSummary,
    'Original failure retained',
  );
  assert.equal(
    result.executions[0].publicationAttempts[0].status,
    'WAITING_FOR_PUBLICATION',
  );
  assert.deepEqual(items, [execution, child()]);
});

test('HTTP retrospective reader paginates exact execution children with only reads', async () => {
  const calls = [];
  const secondHash = 'c'.repeat(64);
  const ddb = {
    async send(command) {
      calls.push(command);
      assert.ok(
        ['GetCommand', 'QueryCommand'].includes(command.constructor.name),
      );
      if (command.constructor.name === 'GetCommand')
        return { Item: command.input.Key.SK === execution.SK ? execution : {} };
      if (command.input.ExpressionAttributeValues[':prefix']) {
        assert.equal(
          command.input.ExpressionAttributeValues[':prefix'],
          `RETROSPECTIVE_FOLLOWUP#${eid}#`,
        );
        assert.equal(command.input.ConsistentRead, true);
        if (!command.input.ExclusiveStartKey)
          return {
            Items: [child()],
            LastEvaluatedKey: { PK: execution.PK, SK: child().SK },
          };
        return {
          Items: [
            child({
              SK: `RETROSPECTIVE_FOLLOWUP#${eid}#${secondHash}`,
              attempt_id: secondHash,
              review_sha256: secondHash,
              status: 'PUBLISHED',
              created_at: now - 5,
              updated_at: now - 1,
            }),
          ],
        };
      }
      return { Items: [] };
    },
  };
  const route = handler(
    'packages/dashboard/server/api/retrospectives/[rcaId]/[executionId].get.ts',
    globals(ddb),
  );
  const result = await httpGet(route);
  assert.equal(result.execution.publicationAttempts.length, 2);
  assert.equal(result.execution.publicationAttempts[0].status, 'PUBLISHED');
  assert.equal(result.execution.retrospectiveStatus, 'FAILED');
  assert.equal(
    calls.filter((c) => c.input.ExpressionAttributeValues?.[':prefix']).length,
    2,
  );
});

for (const [field, value] of [
  ['PK', 'RCA#other'],
  ['rca_id', 'other'],
  ['execution_id', 'other'],
  ['engine', 'headless-codex'],
  ['approval_id', 'other'],
  ['playbook_digest', 'c'.repeat(64)],
  ['ttl', now],
  ['ttl', String(now + 500)],
  ['schema_version', 2],
  ['record_type', 'OTHER'],
  ['review_status', 'FAILED'],
  ['status', 'UNKNOWN'],
  ['attempt_id', 'c'.repeat(64)],
  ['created_at', true],
  ['updated_at', now - 100],
])
  test(`invalid child ${field} cannot attach or imply successful publication`, () => {
    const result = readPublicationHistory(execution, [
      child({ [field]: value }),
    ]);
    assert.equal(result.publicationReadState, 'UNAVAILABLE');
    assert.deepEqual(result.publicationAttempts, []);
  });

test('expired execution and mixed invalid history cannot falsely promote an older success', () => {
  assert.equal(
    readPublicationHistory({ ...execution, ttl: now - 1 }, [child()])
      .publicationReadState,
    'UNAVAILABLE',
  );
  const result = readPublicationHistory(execution, [
    child({ status: 'PUBLISHED' }),
    child({ playbook_digest: 'wrong' }),
  ]);
  assert.equal(result.publicationReadState, 'UNAVAILABLE');
  assert.equal(result.publicationAttempts.length, 1);
});

test('legacy fields remain readable without synthesizing review or publication success', () => {
  const result = readExecution({
    ...execution,
    source_part: undefined,
    retrospective_status: 'UPDATED',
  });
  assert.equal(result.retrospectiveStatus, 'UPDATED');
  assert.deepEqual(result.publicationAttempts, []);
  assert.equal(result.publicationReadState, 'NOT_RECORDED');
  assert.equal(shouldPollPublication(result), false);
});

test('publication polling continues after RESOLVED for waiting and retryable failure, stops for blocked/published/expired', () => {
  for (const status of [
    'WAITING_FOR_PUBLICATION',
    'PUBLISHING',
    'FAILED',
    'BLOCKED',
    'PUBLISHED',
  ]) {
    const result = {
      ...readExecution(execution),
      ...readPublicationHistory(execution, [child({ status })]),
    };
    assert.equal(
      shouldPollPublication(result),
      !['BLOCKED', 'PUBLISHED'].includes(status),
    );
  }
  assert.equal(
    shouldPollPublication({
      state: 'RESOLVED',
      sourcePart: 'recovery',
      retrospectiveStatus: 'COMPLETED',
    }),
    true,
  );
  assert.equal(
    shouldPollPublication({
      ...readExecution(execution),
      ...readPublicationHistory(execution, [child({ ttl: now - 1 })]),
    }),
    false,
  );
});

async function render(executionValue) {
  const vue = nuxtRequire('vue');
  const { parse, compileScript } = nuxtRequire('vue/compiler-sfc');
  const { renderToString } = nuxtRequire('vue/server-renderer');
  const filename = 'packages/dashboard/app/components/PublicationStatus.vue';
  const { descriptor } = parse(
    readFileSync(path.join(root, filename), 'utf8'),
    { filename },
  );
  const compiled = compileScript(descriptor, {
    id: 'publication',
    inlineTemplate: true,
    templateOptions: { ssr: true },
  });
  const mod = { exports: {} };
  const localRequire = (id) =>
    id === '~/utils/publication'
      ? {
          PUBLICATION_LABELS: {
            WAITING_FOR_PUBLICATION: '공용 반영 대기',
            PUBLISHING: '공용 반영 중',
            PUBLISHED: '공용 반영 완료',
            BLOCKED: '공용 반영 차단',
            FAILED: '공용 반영 실패',
          },
        }
      : nuxtRequire(id);
  new Function(
    'require',
    'module',
    'exports',
    'computed',
    transpile(compiled.content),
  )(localRequire, mod, mod.exports, vue.computed);
  return renderToString(
    vue.createSSRApp(mod.exports.default, { execution: executionValue }),
  );
}

test('actual status component shows historical failure and later publication independently, escaping reason text', async () => {
  const state = {
    ...readExecution(execution),
    ...readPublicationHistory(execution, [
      child({ status: 'PUBLISHED', reason: '<script>private()</script>' }),
    ]),
  };
  const rendered = await render(state);
  assert.match(rendered, /원래 회고 기록/);
  assert.match(rendered, /FAILED/);
  assert.match(rendered, /회고 검토 완료/);
  assert.match(rendered, /공용 반영 완료/);
  assert.doesNotMatch(rendered, /<script>/);
  assert.match(rendered, /&lt;script&gt;/);
});

test('HTTP follow-up read failure preserves existing retrospective response with an explicit gap', async () => {
  const ddb = {
    async send(command) {
      if (command.input.ExpressionAttributeValues?.[':prefix'])
        throw Error('temporarily unavailable');
      if (command.constructor.name === 'GetCommand')
        return {
          Item:
            command.input.Key.SK === execution.SK
              ? execution
              : { alarm_name: 'original alarm' },
        };
      return { Items: [] };
    },
  };
  const result = await httpGet(
    handler(
      'packages/dashboard/server/api/retrospectives/[rcaId]/[executionId].get.ts',
      globals(ddb),
    ),
  );
  assert.equal(result.execution.publicationReadState, 'UNAVAILABLE');
  assert.equal(result.execution.state, 'RESOLVED');
  assert.equal(result.execution.retrospectiveStatus, 'FAILED');
  assert.equal(result.issue.alarmName, 'original alarm');
});

for (const status of [
  'WAITING_FOR_PUBLICATION',
  'PUBLISHING',
  'BLOCKED',
  'FAILED',
]) {
  test(`actual view keeps ${status} distinct from publication completion`, async () => {
    const result = await render({
      ...readExecution(execution),
      ...readPublicationHistory(execution, [child({ status })]),
    });
    assert.doesNotMatch(result, /공용 반영 완료/);
    assert.match(result, /원래 회고 기록/);
  });
}

for (const values of [
  { review_sha256: undefined },
  { review_sha256: 'x' },
  { review_sha256: 'e'.repeat(64) },
  { source_part_revision: undefined },
  { source_part_revision: 'e'.repeat(64) },
  { status: 'PUBLISHED', public_playbook_id: undefined },
  { status: 'PUBLISHED', public_playbook_id: '  ' },
  { status: 'PUBLISHED', published_revision: undefined },
  { status: 'PUBLISHED', published_revision: '' },
]) {
  test(`mandatory follow-up identity/publication fields reject ${JSON.stringify(values)}`, () => {
    const result = readPublicationHistory(execution, [child(values)]);
    assert.equal(result.publicationReadState, 'UNAVAILABLE');
    assert.deepEqual(result.publicationAttempts, []);
  });
}

test('source revision must match both execution revision and payload hash; legacy absence stays unrecorded', () => {
  for (const invalid of [
    { ...execution, source_part_revision: undefined },
    { ...execution, source_part_payload_sha256: 'f'.repeat(64) },
  ]) {
    assert.equal(
      readPublicationHistory(invalid, [child()]).publicationReadState,
      'UNAVAILABLE',
    );
    assert.equal(
      readPublicationHistory(invalid, []).publicationReadState,
      'NOT_RECORDED',
    );
  }
});
