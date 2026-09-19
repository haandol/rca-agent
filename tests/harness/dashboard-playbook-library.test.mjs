import assert from 'node:assert/strict';
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import test from 'node:test';
import { spawnSync } from 'node:child_process';

import { deploymentBook } from '../../packages/dashboard/tests/fixtures/deployment-contract.mjs';

const root = path.resolve(import.meta.dirname, '../..');
const require = createRequire(
  path.join(root, 'packages/dashboard/package.json'),
);
const ts = require('typescript');
function load(relative, globals = {}, cache = new Map()) {
  const filename = path.resolve(root, relative);
  if (cache.has(filename)) return cache.get(filename);
  const source = readFileSync(filename, 'utf8');
  const code = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      esModuleInterop: true,
    },
  }).outputText;
  const module = { exports: {} };
  const localRequire = (name) =>
    name.startsWith('.')
      ? name.endsWith('.json')
        ? JSON.parse(
            readFileSync(path.resolve(path.dirname(filename), name), 'utf8'),
          )
        : load(
            path.resolve(
              path.dirname(filename),
              name + (name.endsWith('.ts') ? '' : '.ts'),
            ),
            globals,
            cache,
          )
      : require(name);
  new Function('require', 'module', 'exports', ...Object.keys(globals), code)(
    localRequire,
    module,
    module.exports,
    ...Object.values(globals),
  );
  cache.set(filename, module.exports);
  return module.exports;
}
const { createPlaybookLibrary, libraryEmbedKey, domainPlaybook } = load(
  'packages/dashboard/server/utils/playbookLibrary.ts',
);
const NOW = Date.UTC(2026, 8, 15);
const ttl = NOW / 1000 + 90 * 86400;
const clone = (value) => structuredClone(value);
const key = (row) => `${row.PK}\0${row.SK}`;
function fixture() {
  const before = {
    playbook_id: 'historical',
    failure_type: '연결 고갈',
    symptom_pattern: '지연',
    temporary_mitigation: '연결 수 확인',
    tags: ['database'],
    verification_status: 'VERIFIED',
    execution_steps: [
      {
        step_id: 'original',
        action: 'historical action',
        commands: ['historical command'],
        success_criteria: 'original criterion',
      },
    ],
  };
  const after = {
    ...clone(before),
    temporary_mitigation: '연결 수와 소유 프로세스 확인',
  };
  const comparison = {
    status: 'UPDATE_PROPOSED',
    query: '연결 고갈',
    selected_playbook_id: 'historical',
    candidates: [
      {
        playbook_id: 'historical',
        rca_id: 'old',
        engine: 'headless-codex',
        revision: 'analysis:old',
        similarity: 0.9,
        availability: 'AVAILABLE',
        applicable: true,
        rationale: '같은 연결 증상',
      },
    ],
    proposal: {
      proposal_id: 'p1',
      playbook_id: 'historical',
      base_revision: 'analysis:old',
      source_rca_id: 'old',
      source_engine: 'headless-codex',
      before: clone(before),
      after,
      changes: [
        {
          field: 'temporary_mitigation',
          before: before.temporary_mitigation,
          after: after.temporary_mitigation,
        },
      ],
      rationale: '관측된 연결 소유자 정보를 보강',
      evidence: ['report evidence A'],
      state: 'PENDING',
    },
  };
  const current = { playbook_id: 'current', execution_steps: [], comparison };
  const rows = [
    {
      PK: 'RCA#old',
      SK: 'ANALYSIS#SESSION',
      engine: 'headless-codex',
      state: 'COMPLETED',
      playbook_id: 'historical',
      playbook: JSON.stringify(before),
      metric_name: 'DatabaseConnections',
      ttl,
    },
    {
      PK: 'RCA#new',
      SK: 'ANALYSIS#SESSION',
      engine: 'headless-codex',
      state: 'COMPLETED',
      playbook_id: 'current',
      playbook: JSON.stringify(current),
      ttl,
    },
    {
      PK: 'PLAYBOOK_LIBRARY',
      SK: 'historical',
      playbook_json: JSON.stringify(before),
      revision: 'analysis:old',
      source_rca_id: 'old',
      engine: 'headless-codex',
      publication_status: 'PUBLISHED',
      vector_key: 'historical@analysis:old',
      metric_name: 'DatabaseConnections',
      ttl,
    },
  ];
  for (const row of rows) {
    row.created_at = new Date(NOW).toISOString();
    row.updated_at = new Date(NOW).toISOString();
    if (row.PK === 'PLAYBOOK_LIBRARY') row.ttl = NOW / 1000 + 60 * 86400;
  }
  const storedHead = rows.find((row) => row.PK === 'PLAYBOOK_LIBRARY');
  const { playbook_json, ...stateMetadata } = storedHead;
  rows.push({
    ...stateMetadata,
    PK: 'PLAYBOOK_LIBRARY_STATE',
    ttl,
    original_expires_at: storedHead.ttl,
    failure_type: before.failure_type,
    symptom_pattern: before.symptom_pattern,
    tags: before.tags,
    verification_status: before.verification_status,
  });
  const store = new Map(rows.map((row) => [key(row), clone(row)]));
  const calls = [];
  const controls = {
    now: NOW,
    failPublish: false,
    beforeTransaction: null,
    afterVectorPut: null,
    listPages: [],
  };
  const vectors = new Map([
    [
      'historical',
      {
        key: 'historical',
        metadata: {
          playbook_id: 'historical',
          rca_id: 'old',
          engine: 'headless-codex',
        },
      },
    ],
  ]);
  function matches(item, operation) {
    const expr = operation.ConditionExpression;
    if (!expr) return true;
    const names = operation.ExpressionAttributeNames || {};
    const values = operation.ExpressionAttributeValues || {};
    const tokens = expr.match(/[#:]?[A-Za-z_][A-Za-z_0-9]*|<>|<=|>=|[=<>(),]/g);
    let position = 0;
    const take = (token) => {
      assert.equal(tokens[position++], token);
    };
    const value = (token) =>
      token.startsWith(':') ? values[token] : item?.[names[token] || token];
    const equal = (left, right) => {
      try {
        assert.deepEqual(left, right);
        return true;
      } catch {
        return false;
      }
    };
    function atom() {
      const first = tokens[position++];
      if (first === '(') {
        const result = or();
        take(')');
        return result;
      }
      if (first === 'NOT') return !atom();
      if (first === 'attribute_not_exists' || first === 'attribute_exists') {
        take('(');
        const field = tokens[position++];
        take(')');
        return first === 'attribute_exists'
          ? value(field) !== undefined
          : value(field) === undefined;
      }
      const left = value(first);
      const operator = tokens[position++];
      if (operator === 'IN') {
        take('(');
        const candidates = [value(tokens[position++])];
        while (tokens[position] === ',') {
          take(',');
          candidates.push(value(tokens[position++]));
        }
        take(')');
        return candidates.some((candidate) => equal(left, candidate));
      }
      const right = value(tokens[position++]);
      if (left === undefined || right === undefined) return false;
      if (operator === '=') return equal(left, right);
      if (operator === '<>') return !equal(left, right);
      if (operator === '<') return left < right;
      if (operator === '>') return left > right;
      if (operator === '<=') return left <= right;
      if (operator === '>=') return left >= right;
      assert.fail(`unsupported condition operator ${operator}`);
    }
    function and() {
      let result = atom();
      while (tokens[position] === 'AND') {
        take('AND');
        const right = atom();
        result = result && right;
      }
      return result;
    }
    function or() {
      let result = and();
      while (tokens[position] === 'OR') {
        take('OR');
        const right = and();
        result = result || right;
      }
      return result;
    }
    const result = or();
    assert.equal(
      position,
      tokens.length,
      'the fake must evaluate the entire condition',
    );
    return result;
  }
  const clients = {
    ddb: {
      async send(command) {
        const { input } = command;
        calls.push({ name: command.constructor.name, input: clone(input) });
        if (command.constructor.name === 'GetCommand')
          return { Item: clone(store.get(key(input.Key))) };
        if (command.constructor.name === 'QueryCommand') {
          const all = [...store.values()]
            .filter(
              (row) =>
                row.PK === input.ExpressionAttributeValues[':pk'] &&
                (!input.ExpressionAttributeValues[':prefix'] ||
                  row.SK.startsWith(
                    input.ExpressionAttributeValues[':prefix'],
                  )),
            )
            .sort((a, b) => a.SK.localeCompare(b.SK));
          const offset = input.ExclusiveStartKey
            ? all.findIndex((row) => row.SK === input.ExclusiveStartKey.SK) + 1
            : 0;
          const take = input.Limit || 100;
          const page = all.slice(offset, offset + take);
          return {
            Items: clone(page),
            ...(offset + take < all.length
              ? { LastEvaluatedKey: { PK: page.at(-1).PK, SK: page.at(-1).SK } }
              : {}),
          };
        }
        if (command.constructor.name === 'BatchWriteCommand') {
          if (controls.holdDeletes) throw new Error('hold after delete claim');
          for (const requests of Object.values(input.RequestItems))
            for (const request of requests)
              store.delete(key(request.DeleteRequest.Key));
          return {};
        }
        assert.equal(command.constructor.name, 'TransactWriteCommand');
        if (controls.beforeTransaction) {
          const hook = controls.beforeTransaction;
          controls.beforeTransaction = null;
          await hook(store, input);
        }
        const identities = input.TransactItems.map((entry) => {
          const op = entry.Put || entry.Update || entry.ConditionCheck;
          return key(op.Item || op.Key);
        });
        assert.equal(
          new Set(identities).size,
          identities.length,
          'one operation per DynamoDB item',
        );
        for (const entry of input.TransactItems) {
          const op = entry.Put || entry.Update || entry.ConditionCheck;
          if (!matches(store.get(key(op.Item || op.Key)), op))
            throw Object.assign(new Error('CAS failed'), {
              name: 'TransactionCanceledException',
              CancellationReasons: [{ Code: 'ConditionalCheckFailed' }],
            });
        }
        for (const entry of input.TransactItems) {
          if (entry.Put) store.set(key(entry.Put.Item), clone(entry.Put.Item));
          if (entry.Update) {
            const op = entry.Update;
            const item = store.get(key(op.Key));
            const [set, remove] = op.UpdateExpression.replace(
              /^SET /,
              '',
            ).split(' REMOVE ');
            for (const assignment of set.split(', ')) {
              const [field, value] = assignment.split(' = ');
              item[op.ExpressionAttributeNames?.[field] || field] =
                op.ExpressionAttributeValues[value];
            }
            if (remove)
              for (const field of remove.split(', ')) delete item[field];
          }
        }
        return {};
      },
    },
    vectors: {
      async send(command) {
        calls.push({
          name: command.constructor.name,
          input: clone(command.input),
        });
        if (command.constructor.name === 'GetVectorsCommand')
          return {
            vectors: command.input.keys.flatMap((id) =>
              vectors.has(id) ? [clone(vectors.get(id))] : [],
            ),
          };
        if (command.constructor.name === 'ListVectorsCommand')
          return (
            controls.listPages.shift() || { vectors: [...vectors.values()] }
          );
        if (command.constructor.name === 'PutVectorsCommand') {
          if (controls.failPublish) throw new Error('vector offline');
          for (const vector of command.input.vectors)
            vectors.set(vector.key, clone(vector));
          controls.afterVectorPut?.(store);
          return {};
        }
        assert.equal(command.constructor.name, 'DeleteVectorsCommand');
        for (const id of command.input.keys) vectors.delete(id);
        return {};
      },
    },
    embedding: {
      async send(command) {
        calls.push({ name: command.constructor.name, input: command.input });
        return {
          body: new TextEncoder().encode(
            JSON.stringify({ embeddings: { float: [[0.1, 0.2, 0.3]] } }),
          ),
        };
      },
    },
  };
  const service = createPlaybookLibrary({
    ...clients,
    s3: {
      send: async () => {
        throw new Error(
          'Unexpected source read outside injected verified recovery reader',
        );
      },
    },
    readRecovery: async (...args) => {
      if (!controls.readRecovery)
        throw new Error('recovery reader unavailable');
      return controls.readRecovery(...args);
    },
    now: () => controls.now,
    config: {
      dynamodbTableName: 'table',
      s3VectorBucketName: 'bucket',
      s3VectorPlaybookIndex: 'playbook',
      bedrockEmbeddingModelId: 'cohere.embed-v4:0',
      s3EvidenceBucket: 'evidence',
    },
  });
  async function handler(file, { params = {}, query = {}, body } = {}) {
    const endpoint = load(`packages/dashboard/server/api/${file}`, {
      defineEventHandler: (fn) => fn,
      getRouterParam: (_, name) => params[name],
      getQuery: () => query,
      readBody: async () => body,
      usePlaybookLibrary: () => service,
      ...load('packages/dashboard/server/utils/keys.ts'),
      ...load('packages/dashboard/server/utils/fencing.ts'),
      ...load('packages/dashboard/server/utils/execution.ts'),
      ...load('packages/dashboard/server/utils/playbook.ts'),
      ...load('packages/dashboard/server/utils/executionApproval.ts'),
      useDynamoDB: () => clients.ddb,
      useRuntimeConfig: () => ({
        dynamodbTableName: 'table',
        s3ReportBucket: 'reports',
        executionQueueUrl: 'queue',
      }),
      useS3: () => ({
        send: async (command) => {
          calls.push({ name: command.constructor.name, input: command.input });
          return { Contents: [] };
        },
      }),
      useSqs: () => ({
        send: async (command) => {
          calls.push({ name: command.constructor.name, input: command.input });
          return {};
        },
      }),
      createError: (value) =>
        Object.assign(new Error(value.statusMessage), value),
    }).default;
    return endpoint({});
  }
  const act = (action = 'apply') =>
    handler('playbook-proposals/[rcaId]/[proposalId].post.ts', {
      params: { rcaId: 'new', proposalId: 'p1' },
      query: { engine: 'headless-codex' },
      body: { action },
    });
  return {
    before,
    after,
    comparison,
    store,
    calls,
    controls,
    vectors,
    service,
    handler,
    act,
    deleteRca: (id) =>
      handler('sessions/[id].delete.ts', {
        params: { id },
        query: { engine: 'headless-codex' },
      }),
  };
}

test('library and proposal GET handlers are read-only, annotated, filtered and cursor paged', async () => {
  const f = fixture();
  const page = await f.handler('playbook-library/index.get.ts', {
    query: { tag: 'database', limit: 1 },
  });
  assert.equal(page.items.length, 1);
  assert.ok(page.nextCursor);
  const detail = await f.handler('playbook-library/[id].get.ts', {
    params: { id: 'historical' },
  });
  assert.equal(detail.playbook.library_revision, 'analysis:old');
  assert.equal(detail.playbook.source_engine, 'headless-codex');
  assert.equal(detail.playbook.source_rca_id, 'old');
  const proposals = await f.handler('playbook-proposals/[rcaId].get.ts', {
    params: { rcaId: 'new' },
    query: { engine: 'headless-codex' },
  });
  assert.equal(proposals.comparison.selected_playbook_id, 'historical');
  assert.equal(proposals.comparison.proposal.state, 'PENDING');
  assert.ok(f.calls.every((call) => /^(Get|Query|List)/.test(call.name)));
  await assert.rejects(
    () => f.service.list({ cursor: page.nextCursor, tag: 'other' }),
    { statusCode: 400 },
  );
});

test('actual apply handler preserves historical runbook and current report, publishes once, and overlays disposition', async () => {
  const f = fixture();
  const originalSource = clone(f.store.get('RCA#new\0ANALYSIS#SESSION'));
  const result = await f.act();
  assert.equal(result.comparison.proposal.state, 'APPLIED');
  assert.equal(result.comparison.proposal.publication_status, 'PUBLISHED');
  const head = f.store.get('PLAYBOOK_LIBRARY\0historical');
  assert.equal(head.revision, 'proposal:p1');
  assert.equal(
    f.store.get('PLAYBOOK#historical\0proposal:p1').publication_status,
    'PENDING',
    'the immutable snapshot retains its original publication metadata',
  );
  assert.deepEqual(
    JSON.parse(head.playbook_json).execution_steps,
    f.before.execution_steps,
  );
  assert.equal(
    JSON.parse(head.playbook_json).temporary_mitigation,
    f.after.temporary_mitigation,
  );
  assert.deepEqual(f.store.get('RCA#new\0ANALYSIS#SESSION'), originalSource);
  assert.ok(f.vectors.has('historical@proposal:p1'));
  assert.equal(
    f.vectors.get('historical@proposal:p1').metadata.library_revision,
    'proposal:p1',
  );
  const writes = f.calls.filter(
    (call) => call.name === 'TransactWriteCommand',
  ).length;
  await f.act();
  assert.equal(
    f.calls.filter((call) => call.name === 'TransactWriteCommand').length,
    writes,
  );
  await assert.rejects(() => f.act('reject'), { statusCode: 409 });
});

test('reject is terminal and never publishes or changes the library', async () => {
  const f = fixture();
  const head = clone(f.store.get('PLAYBOOK_LIBRARY\0historical'));
  assert.equal((await f.act('reject')).comparison.proposal.state, 'REJECTED');
  await f.act('reject');
  assert.deepEqual(f.store.get('PLAYBOOK_LIBRARY\0historical'), head);
  assert.ok(
    !f.calls.some(
      (call) =>
        call.name === 'PutVectorsCommand' || call.name === 'InvokeModelCommand',
    ),
  );
  await assert.rejects(() => f.act(), { statusCode: 409 });
});

test('publication failure retains APPLIED/PENDING and same request retries only publication', async () => {
  const f = fixture();
  f.controls.failPublish = true;
  const result = await f.act();
  assert.equal(result.comparison.proposal.state, 'APPLIED');
  assert.equal(result.comparison.proposal.publication_status, 'PENDING');
  assert.ok(result.comparison.proposal.publication_error);
  assert.equal(
    (await f.service.detail('historical')).item.availability,
    'UNAVAILABLE',
  );
  f.controls.failPublish = false;
  assert.equal(
    (await f.act()).comparison.proposal.publication_status,
    'PUBLISHED',
  );
  const headWrites = f.calls.filter(
    (call) =>
      call.name === 'TransactWriteCommand' &&
      call.input.TransactItems.some(
        (entry) => entry.Put?.Item.PK === 'PLAYBOOK_LIBRARY',
      ),
  );
  assert.equal(headWrites.length, 1);
});

test('stale revision or source changed between read and transaction rejects without overwrite', async () => {
  for (const target of [
    'PLAYBOOK_LIBRARY\0historical',
    'RCA#old\0ANALYSIS#SESSION',
    'RCA#new\0ANALYSIS#SESSION',
  ]) {
    const f = fixture();
    f.controls.beforeTransaction = (store) => {
      store.get(target).playbook_json = 'concurrent value';
      if (target.includes('SESSION')) store.get(target).state = 'CANCELLED';
      else store.get(target).revision = 'newer';
    };
    await assert.rejects(() => f.act(), { statusCode: 409 });
    assert.ok(!f.store.has('RCA#new\0headless-codex#PLAYBOOK_PROPOSAL#p1'));
    assert.ok(!f.calls.some((call) => call.name === 'PutVectorsCommand'));
  }
});

test('late publication cannot mark a newer head published or overwrite its immutable vector', async () => {
  const f = fixture();
  f.controls.afterVectorPut = (store) =>
    Object.assign(store.get('PLAYBOOK_LIBRARY\0historical'), {
      revision: 'retrospective:newer',
      vector_key: 'historical@retrospective:newer',
      publication_status: 'PENDING',
    });
  const result = await f.act();
  assert.equal(result.comparison.proposal.publication_status, 'PENDING');
  assert.equal(
    f.store.get('PLAYBOOK_LIBRARY\0historical').revision,
    'retrospective:newer',
  );
  assert.equal(
    f.store.get('PLAYBOOK_LIBRARY\0historical').publication_status,
    'PENDING',
  );
  assert.ok(!f.calls.some((call) => call.name === 'DeleteVectorsCommand'));
});

test('legacy hydration validates latest complete source, deduplicates heads, follows short vector pages', async () => {
  const f = fixture();
  f.store.delete('PLAYBOOK_LIBRARY\0historical');
  f.store.delete('PLAYBOOK_LIBRARY_STATE\0historical');
  const detail = await f.service.detail('historical');
  assert.equal(detail.item.revision, 'legacy');
  assert.equal(detail.item.availability, 'AVAILABLE');
  const heads = await f.service.list({});
  f.controls.listPages.push(
    { vectors: [], nextToken: 'short' },
    { vectors: [...f.vectors.values()] },
  );
  const short = await f.service.list({ cursor: heads.nextCursor });
  assert.equal(short.items.length, 0);
  assert.ok(short.nextCursor);
  const final = await f.service.list({ cursor: short.nextCursor });
  assert.equal(final.items[0].playbook_id, 'historical');
  f.store.set('RCA#old\0headless-codex#PLAYBOOK_REVISION', {
    PK: 'RCA#old',
    SK: 'headless-codex#PLAYBOOK_REVISION',
    playbook_id: 'historical',
    playbook: '{broken',
    ttl,
  });
  // A malformed known current revision cannot fall back to older content.
  const latest = await f.service.detail('historical');
  assert.equal(latest.item.availability, 'UNAVAILABLE');
});

test('knowledge proposals cannot change a runbook or silently drop historical fields', async () => {
  for (const mutation of [
    (proposal) => (proposal.after.execution_steps = []),
    (proposal) => delete proposal.after.tags,
  ]) {
    const f = fixture();
    const session = f.store.get('RCA#new\0ANALYSIS#SESSION');
    const book = JSON.parse(session.playbook);
    mutation(book.comparison.proposal);
    session.playbook = JSON.stringify(book);
    await assert.rejects(() => f.act(), { statusCode: 409 });
    assert.ok(!f.calls.some((call) => call.name === 'TransactWriteCommand'));
  }
});

test('embedding text uses Python Unicode slicing and exact field labels, empty fields omitted', () => {
  assert.equal(
    libraryEmbedKey(
      { failure_type: ' 유형 ', symptom_pattern: ' 증상 ' },
      ' 메트릭 ',
    ),
    '장애유형: 유형 | 증상: 증상 | 메트릭: 메트릭',
  );
  assert.equal(
    libraryEmbedKey({ symptom_pattern: '😀'.repeat(81) }, ''),
    `증상: ${'😀'.repeat(80)}`,
  );
  assert.deepEqual(
    domainPlaybook({
      playbook_id: 'p',
      comparison: {},
      library_revision: 'r',
      source_engine: 'e',
      source_rca_id: 'r',
      stage: 's',
      summary: 's',
      output_summary: 's',
    }),
    { playbook_id: 'p' },
  );
});

test('legacy Headless vector without engine resolves neutral source ownership for detail, list and apply', async () => {
  const f = fixture();
  f.store.delete('PLAYBOOK_LIBRARY\0historical');
  f.store.delete('PLAYBOOK_LIBRARY_STATE\0historical');
  delete f.vectors.get('historical').metadata.engine;
  const session = f.store.get('RCA#new\0ANALYSIS#SESSION');
  const current = JSON.parse(session.playbook);
  current.comparison.proposal.base_revision = 'legacy';
  current.comparison.candidates[0].revision = 'legacy';
  session.playbook = JSON.stringify(current);
  const detail = await f.handler('playbook-library/[id].get.ts', {
    params: { id: 'historical' },
  });
  assert.equal(detail.item.engine, 'headless-codex');
  assert.equal(detail.item.availability, 'AVAILABLE');
  const first = await f.service.list({});
  const legacy = await f.service.list({ cursor: first.nextCursor });
  assert.equal(legacy.items[0].engine, 'headless-codex');
  assert.equal(
    (await f.act()).comparison.proposal.publication_status,
    'PUBLISHED',
  );
  assert.equal(
    f.store.get('PLAYBOOK_LIBRARY\0historical').engine,
    'headless-codex',
  );
});

test('legacy inference uses exact completed legacy sessions and cannot be selected by a caller hint', async () => {
  const f = fixture();
  f.store.delete('PLAYBOOK_LIBRARY\0historical');
  f.store.delete('PLAYBOOK_LIBRARY_STATE\0historical');
  delete f.vectors.get('historical').metadata.engine;
  const original = f.store.get('RCA#old\0ANALYSIS#SESSION');
  f.store.delete('RCA#old\0ANALYSIS#SESSION');
  f.store.set('RCA#old\0headless-codex#SESSION', {
    ...original,
    SK: 'headless-codex#SESSION',
  });
  assert.equal(
    (await f.service.detail('historical')).item.engine,
    'headless-codex',
  );
  f.store.set('RCA#old\0strands#SESSION', {
    ...original,
    engine: 'strands',
    SK: 'strands#SESSION',
  });
  assert.equal(
    (await f.service.detail('historical', { engine: 'headless-codex' })).item
      .availability,
    'UNAVAILABLE',
  );
});

test('published canonical retrospective requires the matching committed revision and complete body', async () => {
  for (const variant of [
    'missing',
    'stage',
    'wrong-token',
    'wrong-body',
    'broken',
    'valid',
  ]) {
    const f = fixture();
    const head = f.store.get('PLAYBOOK_LIBRARY\0historical');
    Object.assign(head, {
      revision: 'retrospective:exec1',
      vector_key: 'historical@retrospective:exec1',
    });
    f.store.get('PLAYBOOK_LIBRARY_STATE\0historical').revision =
      'retrospective:exec1';
    if (variant !== 'missing') {
      const SK =
        variant === 'stage'
          ? 'headless-codex#PLAYBOOK_REVISION_STAGE#exec1'
          : 'headless-codex#PLAYBOOK_REVISION';
      f.store.set(`RCA#old\0${SK}`, {
        PK: 'RCA#old',
        SK,
        playbook_id: 'historical',
        publication_status: variant === 'stage' ? 'PENDING' : 'PUBLISHED',
        revised_by_execution_id: variant === 'wrong-token' ? 'exec2' : 'exec1',
        playbook:
          variant === 'broken'
            ? '{'
            : JSON.stringify({
                ...f.before,
                ...(variant === 'wrong-body'
                  ? { temporary_mitigation: 'changed' }
                  : {}),
              }),
        ttl,
      });
    }
    const detail = await f.service.detail('historical');
    assert.equal(
      detail.item.availability,
      variant === 'valid' ? 'AVAILABLE' : 'UNAVAILABLE',
      variant,
    );
  }
});

test('comparison reads the frozen completed analysis even if a later runbook revision is unreadable', async () => {
  const f = fixture();
  const session = f.store.get('RCA#new\0ANALYSIS#SESSION');
  session.completion_playbook = session.playbook;
  delete session.playbook;
  f.store.set('RCA#new\0headless-codex#PLAYBOOK_REVISION', {
    PK: 'RCA#new',
    SK: 'headless-codex#PLAYBOOK_REVISION',
    playbook_id: 'current',
    playbook: '{broken',
    ttl,
  });
  assert.equal(
    (await f.service.readProposal('new', 'headless-codex')).comparison.proposal
      .proposal_id,
    'p1',
  );
});

test('creating even an empty retrospective record during legacy apply fails its absence guard', async () => {
  const f = fixture();
  f.store.delete('PLAYBOOK_LIBRARY\0historical');
  f.store.delete('PLAYBOOK_LIBRARY_STATE\0historical');
  const session = f.store.get('RCA#new\0ANALYSIS#SESSION');
  const current = JSON.parse(session.playbook);
  current.comparison.proposal.base_revision = 'legacy';
  session.playbook = JSON.stringify(current);
  f.controls.beforeTransaction = (store) =>
    store.set('RCA#old\0headless-codex#PLAYBOOK_REVISION', {
      PK: 'RCA#old',
      SK: 'headless-codex#PLAYBOOK_REVISION',
    });
  await assert.rejects(() => f.act(), { statusCode: 409 });
  assert.ok(!f.store.has('PLAYBOOK_LIBRARY\0historical'));
});

test('pending manual publication blocks deletion of both the proposal RCA and canonical source RCA', async () => {
  const f = fixture();
  for (const id of ['new', 'old'])
    f.store.get(`RCA#${id}\0ANALYSIS#SESSION`).playbook_index_status =
      'PUBLISHED';
  f.controls.failPublish = true;
  await f.act();
  assert.equal(
    f.store.get('PLAYBOOK_LIBRARY\0historical').proposal_rca_id,
    'new',
  );
  for (const id of ['new', 'old']) {
    await assert.rejects(() => f.deleteRca(id), { statusCode: 409 });
    assert.ok(f.store.has(`RCA#${id}\0ANALYSIS#SESSION`));
    assert.equal(
      f.store.get(`RCA#${id}\0ANALYSIS#SESSION`).deleting_at,
      undefined,
    );
  }
  assert.ok(!f.calls.some((call) => call.name === 'BatchWriteCommand'));
  f.controls.failPublish = false;
  assert.equal(
    (await f.act()).comparison.proposal.publication_status,
    'PUBLISHED',
  );
});

test('apply winning after delete reads its targets prevents the delete claim from committing', async () => {
  for (const id of ['new', 'old']) {
    const f = fixture();
    f.controls.failPublish = true;
    f.controls.beforeTransaction = async () => {
      await f.act();
    };
    await assert.rejects(() => f.deleteRca(id), { statusCode: 409 });
    assert.equal(
      f.store.get(`RCA#${id}\0ANALYSIS#SESSION`).deleting_at,
      undefined,
    );
    assert.equal(
      f.store.get('PLAYBOOK_LIBRARY\0historical').publication_status,
      'PENDING',
    );
    assert.ok(f.store.has('RCA#new\0headless-codex#PLAYBOOK_PROPOSAL#p1'));
  }
});

test('delete claim winning after apply reads its baseline prevents publication and disposition creation', async () => {
  for (const id of ['new', 'old']) {
    const f = fixture();
    f.controls.holdDeletes = true;
    f.controls.beforeTransaction = async () => {
      await assert.rejects(() => f.deleteRca(id), /hold after delete claim/);
    };
    await assert.rejects(() => f.act(), { statusCode: 409 });
    assert.ok(f.store.get(`RCA#${id}\0ANALYSIS#SESSION`).deleting_at);
    assert.equal(
      f.store.get('PLAYBOOK_LIBRARY\0historical').revision,
      'analysis:old',
    );
    assert.ok(!f.store.has('RCA#new\0headless-codex#PLAYBOOK_PROPOSAL#p1'));
    assert.ok(!f.calls.some((call) => call.name === 'PutVectorsCommand'));
  }
});

test('a freshly read deletion claim cannot become an accepted apply baseline', async () => {
  for (const id of ['new', 'old']) {
    const f = fixture();
    f.controls.holdDeletes = true;
    await assert.rejects(() => f.deleteRca(id), /hold after delete claim/);
    await assert.rejects(() => f.act(), { statusCode: 409 });
    assert.equal(
      f.store.get('PLAYBOOK_LIBRARY\0historical').revision,
      'analysis:old',
    );
    assert.ok(!f.store.has('RCA#new\0headless-codex#PLAYBOOK_PROPOSAL#p1'));
  }
});

test('deletion still permits published, absent and non-owning pending heads', async () => {
  for (const variant of ['published', 'absent', 'non-owning pending']) {
    for (const id of ['new', 'old']) {
      const f = fixture();
      if (variant === 'absent') {
        f.store.delete('PLAYBOOK_LIBRARY\0historical');
        f.store.delete('PLAYBOOK_LIBRARY_STATE\0historical');
      }
      if (variant === 'non-owning pending')
        Object.assign(f.store.get('PLAYBOOK_LIBRARY\0historical'), {
          publication_status: 'PENDING',
          source_rca_id: 'someone-else',
          proposal_rca_id: 'another-proposal-rca',
        });
      assert.equal((await f.deleteRca(id)).deleted, true, `${variant}: ${id}`);
      assert.ok(!f.store.has(`RCA#${id}\0ANALYSIS#SESSION`));
    }
  }
});

test('transactional deletion retains the original completion-handoff pending guard', async () => {
  const f = fixture();
  f.store.get('RCA#old\0ANALYSIS#SESSION').playbook_index_status = 'PENDING';
  await assert.rejects(() => f.deleteRca('old'), { statusCode: 409 });
  assert.equal(f.store.get('RCA#old\0ANALYSIS#SESSION').deleting_at, undefined);
  assert.ok(!f.calls.some((call) => call.name === 'BatchWriteCommand'));
});

test('corrupt matching current revision makes GET and approval refuse the original commands', async () => {
  const f = fixture();
  const session = f.store.get('RCA#new\0ANALYSIS#SESSION');
  const original = {
    playbook_id: 'current',
    execution_steps: [
      {
        step_id: 's1',
        action: 'Stop owner',
        success_criteria: 'stopped',
        commands: [
          'aws ecs stop-task --cluster demo --task owner --region us-east-1',
        ],
      },
    ],
  };
  Object.assign(session, {
    playbook: JSON.stringify(original),
    confirmed: true,
    report_s3_key: 'reports/new.md',
  });
  f.store.set('RCA#new\0headless-codex#PLAYBOOK_REVISION', {
    PK: 'RCA#new',
    SK: 'headless-codex#PLAYBOOK_REVISION',
    playbook_id: 'current',
    playbook: '{broken',
    publication_status: 'PUBLISHED',
    ttl,
  });
  await assert.rejects(
    () =>
      f.handler('playbooks/[id].get.ts', {
        params: { id: 'new' },
        query: { engine: 'headless-codex' },
      }),
    { statusCode: 409 },
  );
  const approval = load('packages/dashboard/server/utils/executionApproval.ts');
  await assert.rejects(
    () =>
      f.handler('executions.post.ts', {
        body: {
          rcaId: 'new',
          engine: 'headless-codex',
          approvalId: '12345678-1234-4234-8234-123456789012',
          expectedPlaybookDigest: approval.sha256Hex(
            approval.serializePlaybookSnapshot(original),
          ),
        },
      }),
    { statusCode: 409 },
  );
  assert.ok(
    !f.calls.some((call) =>
      [
        'PutObjectCommand',
        'TransactWriteCommand',
        'SendMessageCommand',
      ].includes(call.name),
    ),
  );
});

test('shared resolver retains absent/different-playbook revision compatibility but rejects a corrupt matching revision for both engines', () => {
  const { resolveCurrentPlaybook } = load(
    'packages/dashboard/server/utils/playbook.ts',
  );
  for (const engine of ['headless-codex', 'strands']) {
    const book = { playbook_id: 'p', execution_steps: [] };
    const session = {
      SK: 'ANALYSIS#SESSION',
      engine,
      state: 'COMPLETED',
      playbook_id: 'p',
      playbook: JSON.stringify(book),
      playbook_span_id: 's',
    };
    const span = {
      SK: `${engine}#SPAN#s`,
      engine,
      span_type: 'PLAYBOOK',
      metadata: book,
    };
    assert.deepEqual(
      resolveCurrentPlaybook([session, span], session, engine).playbook,
      book,
    );
    const revision = {
      SK: `${engine}#PLAYBOOK_REVISION`,
      playbook_id: 'another',
      playbook: '{broken',
    };
    assert.deepEqual(
      resolveCurrentPlaybook([session, span, revision], session, engine)
        .playbook,
      book,
    );
    revision.playbook_id = 'p';
    assert.equal(
      resolveCurrentPlaybook([session, span, revision], session, engine),
      null,
    );
    revision.playbook = JSON.stringify(book);
    assert.equal(
      resolveCurrentPlaybook([session, span, revision], session, engine).source,
      'revision',
    );
  }
});

test('after-image list omissions are rejected without rewriting the displayed proposal', async () => {
  for (const field of [
    'tags',
    'verification_steps',
    'prevention_measures',
    'related_metrics',
  ]) {
    const f = fixture();
    const original = { ...f.before, [field]: ['retained-member'] };
    f.store.get('PLAYBOOK_LIBRARY\0historical').playbook_json =
      JSON.stringify(original);
    f.store.get('RCA#old\0ANALYSIS#SESSION').playbook =
      JSON.stringify(original);
    const session = f.store.get('RCA#new\0ANALYSIS#SESSION');
    const current = JSON.parse(session.playbook);
    Object.assign(current.comparison.proposal, {
      before: original,
      after: { ...original, [field]: ['new-only'] },
      changes: [{ field, before: ['retained-member'], after: ['new-only'] }],
    });
    session.playbook = JSON.stringify(current);
    const saved = session.playbook;
    await assert.rejects(() => f.act(), { statusCode: 409 });
    assert.equal(session.playbook, saved);
    assert.deepEqual(
      JSON.parse(f.store.get('PLAYBOOK_LIBRARY\0historical').playbook_json)[
        field
      ],
      ['retained-member'],
    );
    assert.ok(!f.calls.some((call) => call.name === 'TransactWriteCommand'));
  }
});

test('an already accumulated list after-image commits exactly as reviewed', async () => {
  const f = fixture();
  const session = f.store.get('RCA#new\0ANALYSIS#SESSION');
  const current = JSON.parse(session.playbook);
  const after = { ...f.before, tags: ['database', 'new'] };
  Object.assign(current.comparison.proposal, {
    after,
    changes: [
      { field: 'tags', before: ['database'], after: ['database', 'new'] },
    ],
  });
  session.playbook = JSON.stringify(current);
  await f.act();
  assert.deepEqual(
    JSON.parse(f.store.get('PLAYBOOK_LIBRARY\0historical').playbook_json),
    after,
  );
});

/** Model the producer's split: one fixed 60-day original and a body-free reference in session state. */
function archiveComparison(f, expiresAt = NOW / 1000 + 60 * 86400) {
  const session = f.store.get('RCA#new\0ANALYSIS#SESSION');
  const book = JSON.parse(session.playbook);
  const comparison = book.comparison;
  const SK = 'headless-codex#PLAYBOOK_COMPARISON#original1';
  const reference = {
    status: comparison.status,
    selected_playbook_id: comparison.selected_playbook_id,
    original_sk: SK,
    original_expires_at: expiresAt,
    ...(comparison.proposal
      ? {
          proposal: {
            proposal_id: comparison.proposal.proposal_id,
            state: 'PENDING',
          },
        }
      : {}),
  };
  const row = {
    PK: 'RCA#new',
    SK,
    engine: 'headless-codex',
    comparison_json: JSON.stringify(comparison),
    created_at: new Date(NOW).toISOString(),
    ttl: expiresAt,
  };
  f.store.set(key(row), row);
  book.comparison = reference;
  session.playbook = JSON.stringify(book);
  return { row, comparison, reference };
}

test('thin comparison references read the exact archive and apply writes 60-day bodies plus body-free 90-day state', async () => {
  const f = fixture();
  const { comparison } = archiveComparison(f);
  const read = await f.service.readProposal('new', 'headless-codex');
  assert.deepEqual(read.comparison, comparison);
  await f.act();
  const head = f.store.get('PLAYBOOK_LIBRARY\0historical');
  const snapshot = f.store.get('PLAYBOOK#historical\0proposal:p1');
  const state = f.store.get('PLAYBOOK_LIBRARY_STATE\0historical');
  assert.equal(head.ttl, NOW / 1000 + 60 * 86400);
  assert.equal(snapshot.ttl, head.ttl);
  assert.equal(state.ttl, NOW / 1000 + 90 * 86400);
  assert.equal(state.original_expires_at, head.ttl);
  assert.equal(state.proposal_rca_id, 'new');
  assert.equal(state.publication_status, 'PUBLISHED');
  for (const field of [
    'playbook_json',
    'comparison_json',
    'execution_steps',
    'before',
    'after',
    'temporary_mitigation',
  ])
    assert.ok(!(field in state), field);
  assert.ok(
    !JSON.parse(f.store.get('RCA#new\0ANALYSIS#SESSION').playbook).comparison
      .proposal.before,
  );
});

test('expired or missing comparison archives retain only summary state and cannot apply', async () => {
  for (const variant of [
    'expired reference',
    'expired row',
    'missing row',
    'wrong engine key',
  ]) {
    const f = fixture();
    const { row } = archiveComparison(f);
    if (variant === 'expired reference') f.controls.now = NOW + 61 * 86400000;
    if (variant === 'expired row') row.ttl = NOW / 1000 - 1;
    if (variant === 'missing row') f.store.delete(key(row));
    if (variant === 'wrong engine key') {
      const session = f.store.get('RCA#new\0ANALYSIS#SESSION');
      const book = JSON.parse(session.playbook);
      book.comparison.original_sk = 'strands#PLAYBOOK_COMPARISON#original1';
      session.playbook = JSON.stringify(book);
    }
    const response = await f.service.readProposal('new', 'headless-codex');
    assert.equal(response.comparison, null, variant);
    assert.equal(response.summary.status, 'UPDATE_PROPOSED');
    assert.ok(response.unavailable_reason);
    assert.ok(
      !JSON.stringify(response).includes('연결 수와 소유 프로세스 확인'),
    );
    await assert.rejects(() => f.act(), { statusCode: 409 });
    assert.ok(!f.calls.some((call) => call.name === 'TransactWriteCommand'));
  }
});

test('an applied proposal keeps its 90-day disposition readable after its original expires', async () => {
  const f = fixture();
  archiveComparison(f);
  await f.act();
  f.controls.now = NOW + 61 * 86400000;
  const response = await f.service.readProposal('new', 'headless-codex');
  assert.equal(response.comparison, null);
  assert.equal(response.summary.proposal.state, 'APPLIED');
  assert.equal(response.summary.proposal.publication_status, 'PUBLISHED');
});

test('body expiry keeps the STATE listing unavailable and never resurrects a legacy vector', async () => {
  const f = fixture();
  f.controls.now = NOW + 61 * 86400000;
  f.store.delete('PLAYBOOK_LIBRARY\0historical');
  const detail = await f.service.detail('historical');
  assert.equal(detail.playbook, null);
  assert.equal(detail.item.failure_type, f.before.failure_type);
  assert.equal(detail.item.availability, 'UNAVAILABLE');
  const list = await f.service.list({});
  assert.equal(list.items[0].playbook_id, 'historical');
  assert.equal(list.items[0].availability, 'UNAVAILABLE');
  assert.ok(!f.calls.some((call) => call.name === 'GetVectorsCommand'));
});

test('fresh canonical originals remain readable from an older retained source authority', async () => {
  const f = fixture();
  const source = f.store.get('RCA#old\0ANALYSIS#SESSION');
  source.created_at = source.updated_at = new Date(
    NOW - 61 * 86400000,
  ).toISOString();
  source.ttl = NOW / 1000 + 29 * 86400;
  assert.equal(
    (await f.service.detail('historical')).item.availability,
    'AVAILABLE',
  );
  const head = f.store.get('PLAYBOOK_LIBRARY\0historical');
  head.updated_at = source.updated_at;
  head.created_at = source.created_at;
  head.ttl = NOW / 1000 + 29 * 86400; // Pre-split 90-day body still physically present.
  assert.equal(
    (await f.service.detail('historical')).item.availability,
    'UNAVAILABLE',
  );
});

test('legacy inline originals obey selected completion time and cannot be applied at 61 days', async () => {
  const f = fixture();
  const session = f.store.get('RCA#new\0ANALYSIS#SESSION');
  session.created_at = session.updated_at = new Date(
    NOW - 61 * 86400000,
  ).toISOString();
  session.ttl = NOW / 1000 + 29 * 86400;
  assert.equal(
    (await f.service.readProposal('new', 'headless-codex')).comparison,
    null,
  );
  await assert.rejects(() => f.act(), { statusCode: 409 });
  session.updated_at = new Date(NOW).toISOString();
  assert.equal(
    (await f.service.readProposal('new', 'headless-codex')).comparison,
    null,
    'an enclosing state update cannot redate the same original',
  );
  session.completed_at = new Date(NOW).toISOString();
  assert.equal(
    (await f.service.readProposal('new', 'headless-codex')).comparison.status,
    'UPDATE_PROPOSED',
  );
});

test('the archived original is part of the atomic apply guard', async () => {
  const f = fixture();
  const { row } = archiveComparison(f);
  f.controls.beforeTransaction = () => {
    row.comparison_json = '{}';
  };
  await assert.rejects(() => f.act(), { statusCode: 409 });
  assert.equal(
    f.store.get('PLAYBOOK_LIBRARY\0historical').revision,
    'analysis:old',
  );
  assert.equal(
    f.store.get('PLAYBOOK_LIBRARY_STATE\0historical').revision,
    'analysis:old',
  );
});

test('pending STATE protects source and proposal RCA even after the 60-day HEAD disappears', async () => {
  const f = fixture();
  archiveComparison(f);
  f.controls.failPublish = true;
  await f.act();
  f.store.delete('PLAYBOOK_LIBRARY\0historical');
  assert.equal(
    f.store.get('PLAYBOOK_LIBRARY_STATE\0historical').publication_status,
    'PENDING',
  );
  for (const id of ['old', 'new'])
    await assert.rejects(() => f.deleteRca(id), { statusCode: 409 });
});

test('known unpublished or expired matching current revisions cannot queue an approval', async () => {
  for (const metadata of [
    { publication_status: 'PENDING' },
    { publication_status: 'FAILED' },
    { publication_status: 'PUBLISHED', ttl: 1 },
  ]) {
    const f = fixture();
    const original = {
      playbook_id: 'current',
      execution_steps: [
        {
          step_id: 's1',
          action: 'Stop owner',
          success_criteria: 'stopped',
          commands: [
            'aws ecs stop-task --cluster demo --task owner --region us-east-1',
          ],
        },
      ],
    };
    Object.assign(f.store.get('RCA#new\0ANALYSIS#SESSION'), {
      playbook: JSON.stringify(original),
      confirmed: true,
      report_s3_key: 'reports/new.md',
    });
    f.store.set('RCA#new\0headless-codex#PLAYBOOK_REVISION', {
      PK: 'RCA#new',
      SK: 'headless-codex#PLAYBOOK_REVISION',
      playbook_id: 'current',
      playbook: JSON.stringify(original),
      ...metadata,
    });
    await assert.rejects(
      () =>
        f.handler('playbooks/[id].get.ts', {
          params: { id: 'new' },
          query: { engine: 'headless-codex' },
        }),
      { statusCode: 409 },
    );
    const approval = load(
      'packages/dashboard/server/utils/executionApproval.ts',
    );
    await assert.rejects(
      () =>
        f.handler('executions.post.ts', {
          body: {
            rcaId: 'new',
            engine: 'headless-codex',
            approvalId: '12345678-1234-4234-8234-123456789012',
            expectedPlaybookDigest: approval.sha256Hex(
              approval.serializePlaybookSnapshot(original),
            ),
          },
        }),
      { statusCode: 409 },
    );
    assert.ok(
      !f.calls.some((call) =>
        [
          'PutObjectCommand',
          'TransactWriteCommand',
          'SendMessageCommand',
        ].includes(call.name),
      ),
    );
  }
});

test('late publication retries never extend original or initial-apply state expiry', async () => {
  const f = fixture();
  const archived = archiveComparison(f);
  archived.row.ttl = NOW / 1000 + 10 * 86400;
  f.controls.failPublish = true;
  await f.act();
  const head = clone(f.store.get('PLAYBOOK_LIBRARY\0historical'));
  const state = clone(f.store.get('PLAYBOOK_LIBRARY_STATE\0historical'));
  assert.equal(head.original_created_at, new Date(NOW).toISOString());
  assert.equal(head.ttl, archived.row.ttl);
  f.controls.now = NOW + 5 * 86400000;
  f.controls.failPublish = false;
  assert.equal(
    (await f.act()).comparison.proposal.publication_status,
    'PUBLISHED',
  );
  assert.equal(f.store.get('PLAYBOOK_LIBRARY\0historical').ttl, head.ttl);
  assert.equal(
    f.store.get('PLAYBOOK_LIBRARY\0historical').original_created_at,
    head.original_created_at,
  );
  assert.equal(
    f.store.get('PLAYBOOK_LIBRARY_STATE\0historical').ttl,
    state.ttl,
  );
  assert.equal(f.store.get('PLAYBOOK#historical\0proposal:p1').ttl, head.ttl);
});

test('pre-split heads without STATE remain visible without a hidden migration write', async () => {
  const f = fixture();
  f.store.delete('PLAYBOOK_LIBRARY_STATE\0historical');
  const head = f.store.get('PLAYBOOK_LIBRARY\0historical');
  head.ttl = NOW / 1000 + 90 * 86400;
  const first = await f.service.list({});
  assert.equal(first.items[0].playbook_id, 'historical');
  assert.equal(first.items[0].availability, 'AVAILABLE');
  assert.ok(!f.store.has('PLAYBOOK_LIBRARY_STATE\0historical'));
  assert.ok(!f.calls.some((call) => /Write|Update|Put/.test(call.name)));
  f.controls.now = NOW + 61 * 86400000;
  assert.equal(
    (await f.service.detail('historical')).item.availability,
    'UNAVAILABLE',
  );
});

test('approval winning after deletion reads blocks the delete claim atomically through EXEC_ACTIVE', async () => {
  const f = fixture();
  f.controls.beforeTransaction = (store) => {
    store.set('RCA#new\0EXEC_ACTIVE', {
      PK: 'RCA#new',
      SK: 'EXEC_ACTIVE',
      execution_id: 'racing-approval',
      engine: 'headless-codex',
    });
  };
  await assert.rejects(f.deleteRca('new'), { statusCode: 409 });
  const transaction = f.calls.find(
    (call) => call.name === 'TransactWriteCommand',
  );
  assert.ok(
    transaction.input.TransactItems.some(
      (item) =>
        item.ConditionCheck?.Key.SK === 'EXEC_ACTIVE' &&
        item.ConditionCheck.ConditionExpression === 'attribute_not_exists(PK)',
    ),
  );
  assert.ok(
    !f.calls.some((call) =>
      ['BatchWriteCommand', 'DeleteObjectsCommand'].includes(call.name),
    ),
  );
});

/** Export actual handler results for the worker integration, never fabricate a positive binding. */
function matchedRecoveryFixture() {
  const f = fixture();
  const privateBook = {
    ...deploymentBook(),
    rca_id: 'new',
    playbook_id: 'private-recovery',
  };
  for (const book of [f.before, f.after]) {
    book.execution_steps = clone(privateBook.execution_steps);
    book.rollback_context = clone(privateBook.rollback_context);
  }
  f.comparison.proposal.before = clone(f.before);
  f.comparison.proposal.after = clone(f.after);
  const origin = f.store.get('RCA#old\0ANALYSIS#SESSION');
  origin.playbook = JSON.stringify(f.before);
  const current = f.store.get('RCA#new\0ANALYSIS#SESSION');
  current.playbook_id = 'historical';
  current.workflow = 'recovery-first-v1';
  current.playbook_index_status = 'PUBLISHED';
  current.playbook = JSON.stringify({
    ...clone(f.after),
    playbook_id: 'historical',
    comparison: f.comparison,
  });
  f.store.get('PLAYBOOK_LIBRARY\0historical').playbook_json = JSON.stringify(
    f.before,
  );
  const revision = 'c'.repeat(64);
  const record = {
    PK: 'RCA#new',
    SK: 'headless-codex#ANALYSIS_PART#recovery',
    engine: 'headless-codex',
    status: 'COMPLETED',
    approval_status: 'READY',
    revision,
    payload_sha256: revision,
    payload_s3_key: `analysis-parts/headless-codex/new/recovery/${revision}.json`,
    body_expires_at: NOW / 1000 + 60 * 86400,
    ttl,
  };
  f.store.set(key(record), record);
  f.controls.readRecovery = async (items, rcaId, engine) => {
    assert.equal(rcaId, 'new');
    assert.equal(engine, 'headless-codex');
    assert.deepEqual(
      items.find((row) => row.SK === record.SK),
      record,
    );
    return { playbook: clone(privateBook), record: clone(record), revision };
  };
  return { ...f, privateBook, recoveryRecord: record };
}

test('user apply exposes only committed exact canonical results for pending/rejected/matching/mismatching worker decisions', async () => {
  const outputs = {};
  for (const mode of [
    'pending',
    'rejected',
    'applied_matching',
    'applied_mismatch',
  ]) {
    const f = matchedRecoveryFixture();
    if (mode === 'applied_mismatch')
      f.privateBook.execution_steps[0].success_criteria =
        'different approved criterion';
    const untouchedPrivate = clone(f.privateBook);
    if (mode === 'pending')
      await f.service.readProposal('new', 'headless-codex');
    else {
      const response = await f.act(mode === 'rejected' ? 'reject' : 'apply');
      assert.equal(
        response.recoveryBinding.status,
        mode === 'applied_matching' ? 'BOUND' : 'BLOCKED',
      );
    }
    const head = f.store.get('PLAYBOOK_LIBRARY\0historical');
    const disposition = f.store.get(
      'RCA#new\0headless-codex#PLAYBOOK_PROPOSAL#p1',
    );
    assert.deepEqual(
      f.privateBook,
      untouchedPrivate,
      'knowledge apply never changes the retained approval',
    );
    if (mode.startsWith('applied')) {
      assert.equal(disposition.state, 'APPLIED');
      assert.equal(disposition.publication_status, 'PUBLISHED');
      assert.equal(head.revision, 'proposal:p1');
      assert.equal(head.publication_status, 'PUBLISHED');
      assert.deepEqual(
        JSON.parse(head.playbook_json).execution_steps,
        f.before.execution_steps,
      );
      if (mode === 'applied_mismatch')
        assert.notDeepEqual(
          JSON.parse(head.playbook_json).execution_steps,
          f.privateBook.execution_steps,
        );
      else
        assert.deepEqual(
          JSON.parse(head.playbook_json).execution_steps,
          f.privateBook.execution_steps,
        );
    } else {
      assert.equal(head.revision, 'analysis:old');
      assert.equal(
        disposition?.state,
        mode === 'rejected' ? 'REJECTED' : undefined,
      );
    }
    assert.ok(!f.calls.some((call) => call.name === 'SendMessageCommand'));
    if (mode === 'applied_matching')
      assert.ok(
        [...f.store.values()].some((row) =>
          row.SK.startsWith('RECOVERY_PUBLICATION#'),
        ),
      );
    else
      assert.ok(
        ![...f.store.values()].some((row) =>
          row.SK.startsWith('RECOVERY_PUBLICATION#'),
        ),
      );
    outputs[mode] = {
      privateBook: f.privateBook,
      rows: [...f.store.values()],
      disposition: disposition ?? null,
    };
  }
  const worker = spawnSync(
    path.join(root, 'packages/headless-codex/.venv/bin/python'),
    [path.join(root, 'packages/dashboard/tests/publication-worker.py')],
    {
      cwd: root,
      env: {
        ...process.env,
        PYTHONPATH: path.join(root, 'packages/headless-codex/src'),
        AWS_EC2_METADATA_DISABLED: 'true',
      },
      input: JSON.stringify(outputs),
      encoding: 'utf8',
      timeout: 60000,
    },
  );
  assert.equal(worker.status, 0, worker.stderr);
  const result = JSON.parse(worker.stdout);
  assert.equal(result.pending.status, 'WAITING_FOR_PUBLICATION');
  assert.equal(result.rejected.status, 'BLOCKED');
  assert.equal(result.applied_matching.status, 'PUBLISHED');
  assert.equal(result.applied_matching.vector_puts, 1);
  assert.equal(result.applied_mismatch.status, 'BLOCKED');
  for (const name of ['pending', 'rejected', 'applied_mismatch'])
    assert.equal(result[name].vector_puts, 0);
  assert.ok(
    Object.values(result).every(
      (value) => value.historical_execution_unchanged,
    ),
  );
  // Preserve actual handler output for cross-package reproduction; no binding is manually inserted.

  if (process.env.DASHBOARD_APPLY_FIXTURE_DIR) {
    mkdirSync(process.env.DASHBOARD_APPLY_FIXTURE_DIR, { recursive: true });
    writeFileSync(
      path.join(
        process.env.DASHBOARD_APPLY_FIXTURE_DIR,
        'actual-dashboard-apply.json',
      ),
      JSON.stringify(outputs, null, 2),
    );
  }
});

test('user apply commits canonical PUBLISHED and exact recovery binding atomically after vector success', async () => {
  const f = matchedRecoveryFixture();
  const result = await f.act();
  assert.equal(result.recoveryBinding.status, 'BOUND');
  const transactions = f.calls.filter((c) => c.name === 'TransactWriteCommand');
  const final = transactions.find((c) =>
    c.input.TransactItems.some((e) =>
      e.Put?.Item.SK.startsWith('RECOVERY_PUBLICATION#'),
    ),
  );
  assert.ok(final);
  assert.ok(
    final.input.TransactItems.some(
      (e) =>
        e.Update?.Key.PK === 'PLAYBOOK_LIBRARY' &&
        e.Update.ExpressionAttributeValues[':published'] === 'PUBLISHED',
    ),
  );
  const partGuard = final.input.TransactItems.find(
    (e) => e.ConditionCheck?.Key.SK === f.recoveryRecord.SK,
  );
  assert.ok(partGuard);
  assert.ok(
    f.calls.findIndex((c) => c.name === 'PutVectorsCommand') <
      f.calls.indexOf(final),
  );
  const binding = clone(
    [...f.store.values()].find((row) =>
      row.SK.startsWith('RECOVERY_PUBLICATION#'),
    ),
  );
  const writes = f.calls.filter(
    (c) => c.name === 'TransactWriteCommand',
  ).length;
  assert.equal((await f.act()).recoveryBinding.status, 'BOUND');
  assert.equal(
    f.calls.filter((c) => c.name === 'TransactWriteCommand').length,
    writes,
  );
  assert.deepEqual(
    [...f.store.values()].find((row) => row.SK === binding.SK),
    binding,
  );
});

test('index failure has no recovery binding; same applied request publishes and binds without new approval', async () => {
  const f = matchedRecoveryFixture();
  f.controls.failPublish = true;
  const first = await f.act();
  assert.equal(first.comparison.proposal.state, 'APPLIED');
  assert.equal(first.comparison.proposal.publication_status, 'PENDING');
  assert.equal(first.recoveryBinding.status, 'WAITING');
  assert.ok(
    ![...f.store.values()].some((row) =>
      row.SK.startsWith('RECOVERY_PUBLICATION#'),
    ),
  );
  f.controls.failPublish = false;
  assert.equal((await f.act()).recoveryBinding.status, 'BOUND');
  assert.ok(!f.calls.some((c) => c.name === 'SendMessageCommand'));
});

test('READY revision changing at the final transaction leaves public publication and binding uncommitted', async () => {
  const f = matchedRecoveryFixture();
  f.controls.afterVectorPut = () => {
    f.controls.beforeTransaction = (store) => {
      store.get(key(f.recoveryRecord)).approval_status = 'UNAVAILABLE';
    };
  };
  const result = await f.act();
  assert.equal(result.comparison.proposal.state, 'APPLIED');
  assert.equal(result.comparison.proposal.publication_status, 'PENDING');
  assert.equal(
    f.store.get('PLAYBOOK_LIBRARY\0historical').publication_status,
    'PENDING',
  );
  assert.ok(
    ![...f.store.values()].some((row) =>
      row.SK.startsWith('RECOVERY_PUBLICATION#'),
    ),
  );
});

test('replaying an already bound apply cannot overwrite a later retrospective head', async () => {
  const f = matchedRecoveryFixture();
  assert.equal((await f.act()).recoveryBinding.status, 'BOUND');
  const head = f.store.get('PLAYBOOK_LIBRARY\0historical');
  head.revision = 'retrospective:later-execution';
  const later = clone(head);
  const writes = f.calls.filter(
    (c) => c.name === 'TransactWriteCommand',
  ).length;
  assert.equal((await f.act()).recoveryBinding.status, 'BOUND');
  assert.deepEqual(f.store.get('PLAYBOOK_LIBRARY\0historical'), later);
  assert.equal(
    f.calls.filter((c) => c.name === 'TransactWriteCommand').length,
    writes,
  );
});

test('posted recovery context cannot select the user-apply binding or rewrite the related runbook', async () => {
  const f = matchedRecoveryFixture();
  const result = await f.handler(
    'playbook-proposals/[rcaId]/[proposalId].post.ts',
    {
      params: { rcaId: 'new', proposalId: 'p1' },
      query: { engine: 'headless-codex' },
      body: {
        action: 'apply',
        recovery_revision: 'evil',
        public_revision: 'latest',
        execution_steps: [{ commands: ['evil'] }],
      },
    },
  );
  assert.equal(result.recoveryBinding.status, 'BOUND');
  const binding = [...f.store.values()].find((row) =>
    row.SK.startsWith('RECOVERY_PUBLICATION#'),
  );
  assert.equal(binding.recovery_revision, f.recoveryRecord.revision);
  assert.equal(binding.public_revision, 'proposal:p1');
  assert.deepEqual(
    JSON.parse(f.store.get('PLAYBOOK_LIBRARY\0historical').playbook_json)
      .execution_steps,
    f.before.execution_steps,
  );
});

test('a conflicting immutable binding is reported blocked and never overwritten', async () => {
  const f = matchedRecoveryFixture();
  await f.act();
  const binding = [...f.store.values()].find((row) =>
    row.SK.startsWith('RECOVERY_PUBLICATION#'),
  );
  binding.public_revision = 'foreign-revision';
  const original = clone(binding);
  const result = await f.act();
  assert.equal(result.recoveryBinding.status, 'BLOCKED');
  assert.deepEqual(f.store.get(key(binding)), original);
});
