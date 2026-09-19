import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import test from 'node:test';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import {
  deploymentBook,
  ecsObservations,
} from '../../packages/dashboard/tests/fixtures/deployment-contract.mjs';
const root = path.resolve(import.meta.dirname, '../..');
const require = createRequire(
  path.join(root, 'packages/dashboard/package.json'),
);
const ts = require('typescript');
function load(relative, globals = {}, cache = new Map()) {
  const file = path.resolve(root, relative);
  if (cache.has(file)) return cache.get(file);
  const source = readFileSync(file, 'utf8');
  const code = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      esModuleInterop: true,
    },
  }).outputText;
  const localRequire = (name) =>
    name.startsWith('.')
      ? name.endsWith('.json')
        ? JSON.parse(
            readFileSync(path.resolve(path.dirname(file), name), 'utf8'),
          )
        : load(
            path.resolve(
              path.dirname(file),
              name + (name.endsWith('.ts') ? '' : '.ts'),
            ),
            globals,
            cache,
          )
      : require(name);
  const module = { exports: {} };
  new Function('require', 'module', 'exports', ...Object.keys(globals), code)(
    localRequire,
    module,
    module.exports,
    ...Object.values(globals),
  );
  cache.set(file, module.exports);
  return module.exports;
}
const util = Object.assign(
  {},
  ...[
    'keys',
    'retrospectivePublication',
    'playbook',
    'executionApproval',
    'analysisParts',
    'deploymentApproval',
  ].map((name) => load(`packages/dashboard/server/utils/${name}.ts`)),
);
for (const name of [
  'execution',
  'readiness',
  'progress',
  'sessionIndex',
  'fencing',
])
  Object.assign(util, load(`packages/dashboard/server/utils/${name}.ts`, util));
Object.assign(util, load('packages/dashboard/server/utils/fencing.ts', util));
const sha = (text) => createHash('sha256').update(text).digest('hex');
const id = 'fixture';
const engine = 'headless-codex';
const approvalId = '12345678-1234-4234-8234-123456789012';
function fixture() {
  const book = deploymentBook();
  const rows = [
    {
      PK: 'RCA#fixture',
      SK: 'ANALYSIS#SESSION',
      engine,
      state: 'HYPOTHESIS_GENERATION',
      confirmed: false,
      workflow: 'recovery-first-v1',
      ttl: 9999999999,
    },
  ];
  const objects = new Map();
  const incident = {
    schema_version: 1,
    rca_id: id,
    engine,
    alarm: {
      AlarmName: 'IngestFailures',
      AlarmArn:
        'arn:aws:cloudwatch:us-east-1:123456789012:alarm:IngestFailures',
      AWSAccountId: '123456789012',
      AlarmDescription: '{"baseline_ref":"retain exact original description"}',
      Trigger: {
        MetricName: 'Failures',
        Namespace: 'Healthcare/Sensor',
        Dimensions: [{ name: 'ServiceName', value: 'logical-writer' }],
        UnknownTriggerField: { retained: true },
      },
      UnknownNotificationField: { retained: ['complete', 'original'] },
    },
    scoping: {},
    observations: {},
    source_artifacts: [],
  };
  function object(part, value) {
    const raw = JSON.stringify(value);
    const hash = sha(raw);
    const key = `analysis-parts/${engine}/${id}/${part}/${hash}.json`;
    objects.set(key, raw);
    return { key, hash };
  }
  const frozen = object('incident', incident);
  rows.push({
    PK: 'RCA#fixture',
    SK: 'INCIDENT_SNAPSHOT',
    schema_version: 1,
    workflow: 'recovery-first-v1',
    rca_id: id,
    engine,
    payload_s3_key: frozen.key,
    payload_sha256: frozen.hash,
    body_expires_at: 9999999999,
    ttl: 9999999999,
  });
  for (const part of ['recovery', 'root_cause', 'operations']) {
    const payload = {
      schema_version: 1,
      workflow: 'recovery-first-v1',
      rca_id: id,
      engine,
      part,
      status: 'COMPLETED',
      incident_ref: { key: frozen.key, sha256: frozen.hash },
      input_refs: [],
      result:
        part === 'recovery'
          ? {
              recommendation: 'ROLLBACK',
              verification: { valid: true },
              playbook: book,
            }
          : { summary: part },
      limitations: [],
    };
    const stored = object(part, payload);
    rows.push({
      PK: 'RCA#fixture',
      SK: `${engine}#ANALYSIS_PART#${part}`,
      schema_version: 1,
      workflow: 'recovery-first-v1',
      rca_id: id,
      engine,
      part,
      status: 'COMPLETED',
      revision: stored.hash,
      payload_s3_key: stored.key,
      payload_sha256: stored.hash,
      incident_s3_key: frozen.key,
      incident_sha256: frozen.hash,
      summary: part,
      body_expires_at: 9999999999,
      ttl: 9999999999,
      ...(part === 'recovery'
        ? {
            approval_status: 'READY',
            runbook_digest: util.sha256Hex(
              util.serializePlaybookSnapshot(book),
            ),
          }
        : {}),
    });
  }
  const events = [];
  let race;
  let sendFailure = false;
  const recovery = rows[2];
  const body = {
    rcaId: id,
    engine,
    approvalId,
    expectedPlaybookDigest: recovery.runbook_digest,
    expectedRecoveryRevision: recovery.revision,
  };
  const s3 = {
    send: async (command) => {
      events.push(command);
      const { Key, Body, Bucket } = command.input;
      assert.equal(
        Bucket,
        Key.startsWith('analysis-parts/') || Key.endsWith('/incident.json')
          ? 'evidence'
          : 'reports',
      );
      if (command.constructor.name === 'PutObjectCommand') {
        assert.equal(command.input.IfNoneMatch, '*');
        objects.set(Key, Buffer.from(Body).toString());
        return {};
      }
      if (!objects.has(Key))
        throw Object.assign(new Error('missing'), { name: 'NoSuchKey' });
      return command.constructor.name === 'HeadObjectCommand'
        ? {}
        : {
            Body: {
              transformToString: async () => objects.get(Key),
              transformToByteArray: async () => Buffer.from(objects.get(Key)),
            },
          };
    },
  };
  const ddb = {
    send: async (command) => {
      events.push(command);
      if (command.constructor.name === 'QueryCommand')
        return { Items: structuredClone(rows) };
      if (command.constructor.name === 'GetCommand')
        return {
          Item: structuredClone(
            rows.find((row) => row.SK === command.input.Key.SK),
          ),
        };
      if (command.constructor.name === 'TransactWriteCommand') {
        race?.();
        const entries = command.input.TransactItems;
        const checks = entries
          .filter((entry) => entry.ConditionCheck)
          .map((entry) => entry.ConditionCheck);
        assert.equal(
          checks.length,
          3,
          'current part, parent and canonical incident are checked atomically with the reservation',
        );
        const values = checks[0].ExpressionAttributeValues;
        assert.match(checks[0].ConditionExpression, /approval_status = :ready/);
        assert.match(checks[0].ConditionExpression, /payload_sha256 = :hash/);
        assert.match(
          checks[1].ConditionExpression,
          /attribute_not_exists\(deleting_at\)/,
        );
        assert.match(checks[1].ConditionExpression, /:cancelled, :outdated/);
        if (
          recovery.revision !== values[':revision'] ||
          recovery.payload_sha256 !== values[':hash'] ||
          recovery.approval_status !== 'READY' ||
          recovery.runbook_digest !== values[':digest'] ||
          recovery.body_expires_at <= Date.now() / 1000 ||
          rows[0].engine !== engine ||
          rows[1].payload_s3_key !==
            checks[2].ExpressionAttributeValues[':key'] ||
          rows[1].payload_sha256 !==
            checks[2].ExpressionAttributeValues[':hash'] ||
          rows[1].engine !== checks[2].ExpressionAttributeValues[':origin'] ||
          ['CANCELLED', 'OUTDATED'].includes(rows[0].state) ||
          'deleting_at' in rows[0] ||
          rows.some((row) => row.SK === 'EXEC_ACTIVE')
        )
          throw Object.assign(new Error('condition failed'), {
            name: 'TransactionCanceledException',
          });
        rows.push(
          ...entries
            .filter((entry) => entry.Put)
            .map((entry) => structuredClone(entry.Put.Item)),
        );
        return {};
      }
      throw new Error('unexpected DDB command');
    },
  };
  const observations = ecsObservations();
  const globals = {
    ...util,
    defineEventHandler: (fn) => fn,
    getRouterParam: () => id,
    getQuery: () => ({ engine }),
    readBody: async () => body,
    createError: (value) =>
      Object.assign(new Error(value.statusMessage), value),
    useRuntimeConfig: () => ({
      dynamodbTableName: 'table',
      s3EvidenceBucket: 'evidence',
      s3ReportBucket: 'reports',
      executionQueueUrl: 'queue',
    }),
    useDynamoDB: () => ddb,
    useS3: () => s3,
    useSqs: () => ({
      send: async (command) => {
        events.push(command);
        if (sendFailure) throw new Error('transport failure');
        return {};
      },
    }),
    useEcs: () => ({
      send: async (command) => {
        events.push(command);
        const key = {
          DescribeTaskDefinitionCommand: 'target',
          DescribeServicesCommand: 'service',
          DescribeTasksCommand: 'tasks',
          ListTasksCommand:
            command.input.desiredStatus === 'RUNNING' ? 'running' : 'pending',
        }[command.constructor.name];
        assert.ok(key);
        return observations[key];
      },
    }),
  };
  return {
    rows,
    objects,
    recovery,
    body,
    book,
    events,
    globals,
    race: (fn) => {
      race = fn;
    },
    failSend: (value) => {
      sendFailure = value;
    },
    route: (name) =>
      load(`packages/dashboard/server/api/${name}`, globals).default({}),
  };
}
test('actual part and playbook routes expose an early source while analysis is active and unconfirmed', async () => {
  const f = fixture();
  const response = await f.route('analysis-parts/[id].get.ts');
  assert.equal(response.parts.length, 3);
  assert.ok(response.parts.every((part) => part.available));
  const book = await f.route('playbooks/[id].get.ts');
  assert.equal(book.source_mode, 'recovery');
  assert.equal(book.recovery_revision, f.recovery.revision);
  assert.equal(book.playbookDigest, f.recovery.runbook_digest);
  assert.equal(f.rows[0].state, 'HYPOTHESIS_GENERATION');
  assert.equal(f.rows[0].confirmed, false);
  assert.ok(
    !f.events.some((e) =>
      [
        'PutObjectCommand',
        'TransactWriteCommand',
        'SendMessageCommand',
      ].includes(e.constructor.name),
    ),
  );
});
for (const [name, mutate] of [
  ['hash', (f) => f.objects.set(f.recovery.payload_s3_key, '{}')],
  [
    'foreign prefix',
    (f) =>
      (f.recovery.payload_s3_key = f.recovery.payload_s3_key.replace(
        '/fixture/',
        '/foreign/',
      )),
  ],
  ['foreign identity', (f) => (f.recovery.rca_id = 'foreign')],
  ['expired', (f) => (f.recovery.body_expires_at = 1)],
  ['revoked', (f) => (f.recovery.approval_status = 'REVOKED')],
  ['missing body', (f) => f.objects.delete(f.recovery.payload_s3_key)],
  ['bad incident', (f) => f.objects.set(f.rows[1].payload_s3_key, '{}')],
  ['bad digest', (f) => (f.recovery.runbook_digest = '0'.repeat(64))],
  ['cancelled parent', (f) => (f.rows[0].state = 'CANCELLED')],
  ['outdated parent', (f) => (f.rows[0].state = 'OUTDATED')],
  ['deleting parent', (f) => (f.rows[0].deleting_at = 'now')],
])
  test(`actual approval rejects ${name} before snapshot/reservation/publication`, async () => {
    const f = fixture();
    mutate(f);
    await assert.rejects(f.route('executions.post.ts'), { statusCode: 409 });
    assert.ok(
      !f.events.some((e) =>
        [
          'PutObjectCommand',
          'TransactWriteCommand',
          'SendMessageCommand',
        ].includes(e.constructor.name),
      ),
    );
  });
test('one unreadable part preserves siblings and cannot fall back to an old completed playbook', async () => {
  const f = fixture();
  f.objects.delete(f.rows[3].payload_s3_key);
  const response = await f.route('analysis-parts/[id].get.ts');
  assert.deepEqual(
    response.parts.map((p) => p.available),
    [true, false, true],
  );
  f.rows[0].state = 'COMPLETED';
  f.rows[0].confirmed = true;
  f.rows[0].playbook = JSON.stringify(f.book);
  f.rows[0].playbook_id = f.book.playbook_id;
  f.recovery.approval_status = 'REVOKED';
  await assert.rejects(f.route('playbooks/[id].get.ts'), { statusCode: 409 });
});
test('actual early approval verifies ECS then stores snapshot then atomically reserves then sends unchanged queue contract', async () => {
  const f = fixture();
  const response = await f.route('executions.post.ts');
  const names = f.events.map((e) => e.constructor.name);
  assert.ok(
    names.indexOf('DescribeServicesCommand') <
      names.indexOf('PutObjectCommand'),
  );
  assert.ok(
    names.indexOf('PutObjectCommand') < names.indexOf('TransactWriteCommand'),
  );
  assert.ok(
    names.indexOf('TransactWriteCommand') < names.indexOf('SendMessageCommand'),
  );
  const execution = f.rows.find((r) => r.SK === `EXEC#${approvalId}`);
  assert.equal(execution.source_part_revision, f.recovery.revision);
  assert.equal(response.playbookDigest, f.recovery.runbook_digest);
  const queue = JSON.parse(
    f.events.find((e) => e.constructor.name === 'SendMessageCommand').input
      .MessageBody,
  );
  assert.equal(queue.source_part, undefined);
  assert.equal(queue.playbook_digest, f.body.expectedPlaybookDigest);
  assert.deepEqual(
    JSON.parse(f.objects.get(queue.approved_playbook_s3_key)),
    f.book,
  );
});
for (const [name, mutate] of [
  ['replaced', (f) => (f.recovery.revision = 'f'.repeat(64))],
  ['revoked', (f) => (f.recovery.approval_status = 'REVOKED')],
  ['cancelled', (f) => (f.rows[0].state = 'CANCELLED')],
  ['deleted', (f) => (f.rows[0].deleting_at = 'now')],
  ['expired', (f) => (f.recovery.body_expires_at = 1)],
])
  test(`atomic reservation defeats ${name} between read and commit`, async () => {
    const f = fixture();
    f.race(() => mutate(f));
    await assert.rejects(f.route('executions.post.ts'), { statusCode: 409 });
    assert.ok(f.events.some((e) => e.constructor.name === 'PutObjectCommand'));
    assert.ok(
      !f.events.some((e) => e.constructor.name === 'SendMessageCommand'),
    );
    assert.ok(!f.rows.some((r) => r.SK === 'EXEC_ACTIVE'));
  });
test('same UUID retransmission uses reserved snapshot after recovery revocation and parent cancellation', async () => {
  const f = fixture();
  f.failSend(true);
  await assert.rejects(f.route('executions.post.ts'), { statusCode: 503 });
  const snapshot = f.objects.get(
    `approvals/fixture/${approvalId}/playbook.json`,
  );
  f.recovery.approval_status = 'REVOKED';
  f.recovery.revision = 'c'.repeat(64);
  f.rows[0].state = 'CANCELLED';
  f.failSend(false);
  f.events.length = 0;
  const response = await f.route('executions.post.ts');
  assert.equal(response.reserved, false);
  assert.equal(
    f.objects.get(`approvals/fixture/${approvalId}/playbook.json`),
    snapshot,
  );
  assert.ok(
    !f.events.some((e) =>
      [
        'TransactWriteCommand',
        'PutObjectCommand',
        'DescribeServicesCommand',
      ].includes(e.constructor.name),
    ),
  );
  f.body.expectedPlaybookDigest = 'd'.repeat(64);
  await assert.rejects(f.route('executions.post.ts'), { statusCode: 409 });
});
test('no workflow and no part records retain legacy discovery', async () => {
  const f = fixture();
  f.rows.splice(1);
  delete f.rows[0].workflow;
  const response = await f.route('analysis-parts/[id].get.ts');
  assert.equal(response.workflow, null);
  assert.deepEqual(response.parts, []);
});

test('engine takeover reads the canonical incident from its original engine while each part remains request-bound', async () => {
  const f = fixture();
  const incidentRecord = f.rows[1];
  const incident = JSON.parse(f.objects.get(incidentRecord.payload_s3_key));
  incident.engine = 'strands';
  const text = JSON.stringify(incident),
    hash = sha(text),
    key = `analysis-parts/strands/fixture/incident/${hash}.json`;
  f.objects.set(key, text);
  incidentRecord.engine = 'strands';
  incidentRecord.payload_s3_key = key;
  incidentRecord.payload_sha256 = hash;
  for (const row of f.rows.slice(2)) {
    const payload = JSON.parse(f.objects.get(row.payload_s3_key));
    payload.incident_ref = { key, sha256: hash };
    const raw = JSON.stringify(payload),
      payloadHash = sha(raw);
    row.payload_sha256 = payloadHash;
    row.revision = payloadHash;
    row.payload_s3_key = `analysis-parts/${engine}/fixture/${row.part}/${payloadHash}.json`;
    row.incident_s3_key = key;
    row.incident_sha256 = hash;
    f.objects.set(row.payload_s3_key, raw);
  }
  const response = await f.route('analysis-parts/[id].get.ts');
  assert.ok(response.parts.every((part) => part.available));
  const book = await f.route('playbooks/[id].get.ts');
  assert.equal(book.source_mode, 'recovery');
  incidentRecord.engine = 'headless-codex';
  await assert.rejects(f.route('playbooks/[id].get.ts'), { statusCode: 409 });
});
test('root failure does not invalidate READY recovery, but workflow marker alone never enables legacy fallback', async () => {
  const f = fixture();
  f.rows[0].state = 'FAILED';
  f.rows[3].status = 'FAILED';
  const book = await f.route('playbooks/[id].get.ts');
  assert.equal(book.source_mode, 'recovery');
  f.rows.splice(1);
  f.rows[0].state = 'COMPLETED';
  f.rows[0].confirmed = true;
  f.rows[0].playbook = JSON.stringify(f.book);
  f.rows[0].playbook_id = f.book.playbook_id;
  await assert.rejects(f.route('playbooks/[id].get.ts'), { statusCode: 409 });
});
test('wrong payload identity with matching bytes/hash is still rejected', async () => {
  const f = fixture();
  const payload = JSON.parse(f.objects.get(f.recovery.payload_s3_key));
  payload.rca_id = 'foreign';
  const text = JSON.stringify(payload),
    hash = sha(text);
  f.recovery.revision = hash;
  f.recovery.payload_sha256 = hash;
  f.recovery.payload_s3_key = `analysis-parts/${engine}/fixture/recovery/${hash}.json`;
  f.objects.set(f.recovery.payload_s3_key, text);
  await assert.rejects(f.route('executions.post.ts'), { statusCode: 409 });
  assert.ok(!f.events.some((e) => e.constructor.name === 'PutObjectCommand'));
});

test('progressive readiness and outcome counts neither hide resolved recovery nor count the incident twice', () => {
  const { countSessionOutcomes } = load(
    'packages/dashboard/app/utils/sessionState.ts',
  );
  const counts = countSessionOutcomes(
    { HYPOTHESIS_GENERATION: 2, FAILED: 1, COMPLETED: 1 },
    [
      {
        state: 'HYPOTHESIS_GENERATION',
        workflow: 'recovery-first-v1',
        executionState: 'RESOLVED',
      },
      {
        state: 'FAILED',
        workflow: 'recovery-first-v1',
        executionState: 'RESOLVED',
      },
      { state: 'COMPLETED', readiness: 'AWAITING_APPROVAL' },
    ],
  );
  assert.equal(counts.get('RESOLVED'), 2);
  assert.equal(counts.get('RUNNING'), 1);
  assert.equal(counts.get('AWAITING'), 1);
  assert.equal(
    [...counts.values()].reduce((a, b) => a + b, 0),
    4,
  );
});
test('partition reads follow all pages before deriving readiness', async () => {
  const inputs = [];
  const ddb = {
    send: async (command) => {
      inputs.push(command.input);
      return inputs.length === 1
        ? {
            Items: [{ SK: 'trace' }],
            LastEvaluatedKey: { PK: 'RCA#fixture', SK: 'trace' },
          }
        : { Items: [{ SK: 'headless-codex#ANALYSIS_PART#recovery' }] };
    },
  };
  const result = await util.readAnalysisPartition(ddb, 'table', 'fixture');
  assert.equal(result.length, 2);
  assert.deepEqual(inputs[1].ExclusiveStartKey, {
    PK: 'RCA#fixture',
    SK: 'trace',
  });
  assert.ok(inputs.every((input) => input.ConsistentRead));
});

/** Re-seal fixture objects exactly as the immutable writer would, without weakening reader hashes. */
function replaceFrozenAlarm(f, alarm) {
  const record = f.rows[1];
  const frozen = JSON.parse(f.objects.get(record.payload_s3_key));
  frozen.alarm = alarm;
  const raw = JSON.stringify(frozen),
    hash = sha(raw);
  record.payload_s3_key = `analysis-parts/${record.engine}/fixture/incident/${hash}.json`;
  record.payload_sha256 = hash;
  f.objects.set(record.payload_s3_key, raw);
  for (const row of f.rows.filter((row) => row.part)) {
    const payload = JSON.parse(f.objects.get(row.payload_s3_key));
    payload.incident_ref = { key: record.payload_s3_key, sha256: hash };
    const body = JSON.stringify(payload),
      revision = sha(body);
    row.revision = revision;
    row.payload_sha256 = revision;
    row.payload_s3_key = `analysis-parts/${engine}/fixture/${row.part}/${revision}.json`;
    row.incident_s3_key = record.payload_s3_key;
    row.incident_sha256 = hash;
    f.objects.set(row.payload_s3_key, body);
  }
  f.body.expectedRecoveryRevision = f.recovery.revision;
}
function originalAlarm(f) {
  return JSON.parse(f.objects.get(f.rows[1].payload_s3_key)).alarm;
}
function useNormalizedFrozenAlarm(f) {
  const original = originalAlarm(f);
  f.rows[0].alarm_name = original.AlarmName;
  f.rows[0].alarm_data = JSON.stringify(original, null, 2);
  replaceFrozenAlarm(f, {
    alarm_name: original.AlarmName,
    alarm_arn: original.AlarmArn,
    alarm_description: original.AlarmDescription,
    trigger: {
      metric_name: original.Trigger.MetricName,
      namespace: original.Trigger.Namespace,
      dimensions: { ServiceName: 'logical-writer' },
    },
  });
  return original;
}
test('approval reserves only immutable incident references while large original alarm fields remain in S3', async () => {
  const f = fixture(),
    original = originalAlarm(f),
    digest = f.body.expectedPlaybookDigest;
  original.UnknownNotificationField.large = 'x'.repeat(450000);
  replaceFrozenAlarm(f, original);
  f.body.source_alarm_name = 'forged';
  f.body.source_alarm_data_json = '{"AlarmName":"forged"}';
  f.body.source_incident_s3_key = 'foreign';
  await f.route('executions.post.ts');
  const reserved = f.rows.find((row) => row.SK === `EXEC#${approvalId}`);
  assert.equal(reserved.source_alarm_name, original.AlarmName);
  assert.equal(reserved.source_alarm_data_json, undefined);
  assert.equal(
    reserved.source_incident_s3_key,
    `approvals/fixture/${approvalId}/incident.json`,
  );
  assert.equal(reserved.original_incident_s3_key, f.rows[1].payload_s3_key);
  assert.equal(reserved.source_incident_sha256, f.rows[1].payload_sha256);
  assert.equal(reserved.source_incident_engine, f.rows[1].engine);
  assert.deepEqual(
    JSON.parse(f.objects.get(reserved.source_incident_s3_key)).alarm,
    original,
  );
  assert.ok(
    Buffer.byteLength(JSON.stringify(reserved)) < 5000,
    'large source JSON is never copied into execution metadata',
  );
  assert.equal(reserved.playbook_digest, digest);
  assert.deepEqual(
    JSON.parse(f.objects.get(reserved.approved_playbook_s3_key)),
    f.book,
  );
  const checks = f.events.find(
    (event) => event.constructor.name === 'TransactWriteCommand',
  ).input.TransactItems;
  assert.equal(checks[2].ConditionCheck.Key.SK, 'INCIDENT_SNAPSHOT');
  assert.equal(
    checks[2].ConditionCheck.ExpressionAttributeValues[':hash'],
    f.rows[1].payload_sha256,
  );
});
test('normalized alarm cannot substitute for full frozen original even if mutable parent retains JSON', async () => {
  const f = fixture();
  useNormalizedFrozenAlarm(f);
  await assert.rejects(f.route('executions.post.ts'), { statusCode: 409 });
  assert.ok(
    !f.events.some((event) =>
      [
        'PutObjectCommand',
        'TransactWriteCommand',
        'SendMessageCommand',
      ].includes(event.constructor.name),
    ),
  );
});
for (const [name, mutate] of [
  [
    'different original alarm',
    (f) => {
      const alarm = originalAlarm(f);
      alarm.AlarmName = 'Different';
      alarm.AlarmArn = alarm.AlarmArn.replace('IngestFailures', 'Different');
      replaceFrozenAlarm(f, alarm);
    },
  ],
  ['missing original alarm', (f) => replaceFrozenAlarm(f, {})],
  [
    'wrong original account',
    (f) => {
      const alarm = originalAlarm(f);
      alarm.AWSAccountId = '999999999999';
      replaceFrozenAlarm(f, alarm);
    },
  ],
  [
    'normalized alarm with mismatched parent description',
    (f) => {
      useNormalizedFrozenAlarm(f);
      const data = JSON.parse(f.rows[0].alarm_data);
      data.AlarmDescription = 'changed';
      f.rows[0].alarm_data = JSON.stringify(data);
    },
  ],
])
  test(`source provenance rejects ${name} before snapshot or reservation`, async () => {
    const f = fixture();
    mutate(f);
    await assert.rejects(f.route('executions.post.ts'), { statusCode: 409 });
    assert.ok(
      !f.events.some((event) =>
        [
          'PutObjectCommand',
          'TransactWriteCommand',
          'SendMessageCommand',
        ].includes(event.constructor.name),
      ),
    );
  });
test('canonical incident replacement during reservation cannot authorize a stale source', async () => {
  const f = fixture();
  f.race(() => {
    f.rows[1].payload_sha256 = 'f'.repeat(64);
  });
  await assert.rejects(f.route('executions.post.ts'), { statusCode: 409 });
  assert.ok(
    !f.events.some((event) => event.constructor.name === 'SendMessageCommand'),
  );
});
test('same UUID retains original incident refs after current parent and canonical head change', async () => {
  const f = fixture();
  f.failSend(true);
  await assert.rejects(f.route('executions.post.ts'), { statusCode: 503 });
  const reserved = f.rows.find((row) => row.SK === `EXEC#${approvalId}`);
  const before = structuredClone(reserved);
  f.rows[0].alarm_name = 'changed';
  f.rows[0].alarm_data = '{}';
  f.rows[1].payload_s3_key = 'changed';
  f.rows[1].payload_sha256 = 'c'.repeat(64);
  f.failSend(false);
  f.events.length = 0;
  await f.route('executions.post.ts');
  assert.deepEqual(reserved, before);
  assert.ok(
    !f.events.some(
      (event) =>
        event.constructor.name === 'GetObjectCommand' &&
        event.input.Key.startsWith('analysis-parts/'),
    ),
  );
});

function priorEngineExecution(state = 'RESOLVED') {
  return {
    PK: 'RCA#fixture',
    SK: 'EXEC#prior-strands',
    execution_id: 'prior-strands',
    rca_id: 'fixture',
    engine: 'strands',
    execution_state: state,
    attempt: 1,
    source_alarm_name: 'IngestFailures',
    report_s3_key: 'analysis-parts/strands/fixture/recovery/source.json',
    source_part: 'recovery',
  };
}
test('new workflow history and session outcome retain prior-engine RESOLVED while current engine analyzes', async () => {
  const f = fixture();
  f.rows.push(priorEngineExecution());
  const history = await f.route('executions/[rcaId].get.ts');
  assert.equal(history.executionScope, 'rca');
  assert.equal(history.executions.length, 1);
  assert.equal(history.executions[0].engine, 'strands');
  assert.equal(history.executions[0].state, 'RESOLVED');
  const session = await f.route('sessions/[id]/index.get.ts');
  assert.equal(session.engine, 'headless-codex');
  assert.equal(session.executionState, 'RESOLVED');
  assert.equal(session.executionEngine, 'strands');
  assert.equal(session.state, 'HYPOTHESIS_GENERATION');
  const { outcomeOf } = load('packages/dashboard/app/utils/sessionState.ts');
  assert.equal(outcomeOf(session), 'RESOLVED');
});
test('new workflow exposes prior-engine active reservation and prioritizes active work over older success', async () => {
  const f = fixture();
  f.rows.push(priorEngineExecution());
  f.rows.push({
    ...priorEngineExecution('PENDING_APPROVAL'),
    SK: 'EXEC#active-strands',
    execution_id: 'active-strands',
    attempt: 0,
  });
  f.rows.push({
    PK: 'RCA#fixture',
    SK: 'EXEC_ACTIVE',
    execution_id: 'active-strands',
    engine: 'strands',
  });
  const history = await f.route('executions/[rcaId].get.ts');
  assert.equal(history.activeExecutionId, 'active-strands');
  assert.equal(history.activeExecutionEngine, 'strands');
  assert.equal(history.executions[0].executionId, 'active-strands');
  const session = await f.route('sessions/[id]/index.get.ts');
  assert.equal(session.executionState, 'PENDING_APPROVAL');
});
test('legacy execution history remains engine-scoped', async () => {
  const f = fixture();
  f.rows.splice(1);
  delete f.rows[0].workflow;
  f.rows.push(priorEngineExecution());
  const history = await f.route('executions/[rcaId].get.ts');
  assert.equal(history.executionScope, 'engine');
  assert.deepEqual(history.executions, []);
});
test('retrospective follows actual execution origin, not the takeover parent root result', async () => {
  const f = fixture();
  f.rows.push(priorEngineExecution());
  f.rows[0].root_cause = 'new engine analysis';
  f.rows[0].confirmed = true;
  f.globals.getRouterParam = (_event, key) =>
    key === 'executionId' ? 'prior-strands' : 'fixture';
  const result = await f.route('retrospectives/[rcaId]/[executionId].get.ts');
  assert.equal(result.execution.engine, 'strands');
  assert.equal(result.issue.engine, 'strands');
  assert.equal(result.issue.alarmName, 'IngestFailures');
  assert.equal(result.issue.rootCause, '');
  assert.equal(result.issue.confirmed, false);
  const query = f.events.find(
    (event) =>
      event.constructor.name === 'QueryCommand' &&
      event.input.ExpressionAttributeValues?.[':sk'],
  );
  assert.equal(
    query.input.ExpressionAttributeValues[':sk'],
    'strands#PLAYBOOK_REVISION',
  );
});

test('list and aggregate routes retain RCA-scoped recovery after engine takeover', async () => {
  const f = fixture();
  f.rows.push(priorEngineExecution());
  const original = f.globals.useDynamoDB();
  f.globals.useDynamoDB = () => ({
    send: async (command) => {
      if (command.input.IndexName)
        return {
          Items:
            command.input.ExpressionAttributeValues[':engine'] === engine
              ? [f.rows[0]]
              : [],
        };
      return original.send(command);
    },
  });
  const list = await f.route('sessions.get.ts');
  assert.equal(list.sessions.length, 1);
  assert.equal(list.sessions[0].engine, engine);
  assert.equal(list.sessions[0].executionState, 'RESOLVED');
  assert.equal(list.sessions[0].executionEngine, 'strands');
  const summary = await f.route('sessions-summary.get.ts');
  assert.equal(summary.total, 1);
  assert.equal(summary.completedOutcomes.length, 1);
  assert.equal(summary.completedOutcomes[0].executionState, 'RESOLVED');
  assert.equal(summary.completedOutcomes[0].state, 'HYPOTHESIS_GENERATION');
});

/** Preserve a valid READY envelope while changing only the proposed plan for adversarial approval tests. */
function resealRecoveryPlan(f) {
  const payload = JSON.parse(f.objects.get(f.recovery.payload_s3_key));
  payload.result.playbook = f.book;
  const raw = JSON.stringify(payload),
    revision = sha(raw);
  f.recovery.revision = revision;
  f.recovery.payload_sha256 = revision;
  f.recovery.payload_s3_key = `analysis-parts/${engine}/fixture/recovery/${revision}.json`;
  f.recovery.runbook_digest = util.sha256Hex(
    util.serializePlaybookSnapshot(f.book),
  );
  f.objects.set(f.recovery.payload_s3_key, raw);
  f.body.expectedRecoveryRevision = revision;
  f.body.expectedPlaybookDigest = f.recovery.runbook_digest;
}
for (const [name, command] of [
  [
    'extra StopTask',
    'aws ecs stop-task --cluster cluster --task extra-task --region us-east-1',
  ],
  [
    'extra service scaling',
    'aws application-autoscaling register-scalable-target --service-namespace ecs --resource-id service/cluster/app --scalable-dimension ecs:service:DesiredCount --min-capacity 1 --max-capacity 2 --region us-east-1',
  ],
])
  test(`early actual approval rejects ${name} even with valid generic steps, human digest and READY metadata`, async () => {
    const f = fixture();
    f.book.execution_steps.push({
      step_id: 'extra-write',
      action: 'Extra action',
      success_criteria: 'action completed',
      commands: [command],
    });
    assert.equal(
      util.validateExecutablePlaybook(f.book).valid,
      true,
      'counterexample is valid under the unchanged legacy/generic contract',
    );
    resealRecoveryPlan(f);
    await assert.rejects(f.route('executions.post.ts'), { statusCode: 409 });
    assert.ok(
      !f.events.some((event) =>
        [
          'PutObjectCommand',
          'TransactWriteCommand',
          'SendMessageCommand',
        ].includes(event.constructor.name),
      ),
    );
  });
test('early actual approval rejects a plan without rollback before snapshot and queue', async () => {
  const f = fixture();
  f.book.execution_steps = [f.book.execution_steps[0]];
  assert.equal(util.validateExecutablePlaybook(f.book).valid, true);
  resealRecoveryPlan(f);
  await assert.rejects(f.route('executions.post.ts'), { statusCode: 409 });
  assert.ok(
    !f.events.some((event) =>
      [
        'PutObjectCommand',
        'TransactWriteCommand',
        'SendMessageCommand',
      ].includes(event.constructor.name),
    ),
  );
});

test('early policy parses operation tokens rather than write-looking text inside readonly arguments', async () => {
  const f = fixture();
  f.book.execution_steps[0].commands.push(
    'aws cloudwatch describe-alarms --alarm-names "stop-task update-service" --region us-east-1',
  );
  resealRecoveryPlan(f);
  const response = await f.route('executions.post.ts');
  assert.equal(response.requested, true);
});
test('early policy rejects a quoted write operation and two otherwise valid pinned rollbacks', async () => {
  for (const kind of ['quoted-write', 'two-rollbacks']) {
    const f = fixture();
    if (kind === 'quoted-write')
      f.book.execution_steps.push({
        step_id: 'quoted',
        action: 'extra',
        success_criteria: 'done',
        commands: [
          "aws ecs 'stop-task' --cluster cluster --task extra-task --region us-east-1",
        ],
      });
    else {
      const duplicate = structuredClone(f.book.execution_steps);
      for (const step of duplicate) {
        step.step_id += '-second';
        if (step.deployment_wait)
          step.deployment_wait.action_step_id += '-second';
        if (step.metric_wait?.deployment_step_id)
          step.metric_wait.deployment_step_id += '-second';
      }
      f.book.execution_steps.push(...duplicate);
    }
    assert.equal(util.validateExecutablePlaybook(f.book).valid, true, kind);
    resealRecoveryPlan(f);
    await assert.rejects(f.route('executions.post.ts'), { statusCode: 409 });
    assert.ok(
      !f.events.some((event) =>
        [
          'PutObjectCommand',
          'TransactWriteCommand',
          'SendMessageCommand',
        ].includes(event.constructor.name),
      ),
    );
  }
});
test('confirmed legacy route preserves its existing operation policy', async () => {
  const f = fixture();
  f.book.execution_steps.push({
    step_id: 'legacy-stop',
    action: 'stop',
    success_criteria: 'stopped',
    commands: [
      'aws ecs stop-task --cluster cluster --task extra-task --region us-east-1',
    ],
  });
  f.rows.splice(1);
  delete f.rows[0].workflow;
  delete f.body.expectedRecoveryRevision;
  Object.assign(f.rows[0], {
    state: 'COMPLETED',
    confirmed: true,
    playbook: JSON.stringify(f.book),
    playbook_id: f.book.playbook_id,
    report_s3_key: 'legacy.md',
  });
  f.body.expectedPlaybookDigest = util.sha256Hex(
    util.serializePlaybookSnapshot(f.book),
  );
  f.objects.set('legacy.md', '# legacy report');
  const ddb = f.globals.useDynamoDB();
  f.globals.useDynamoDB = () => ({
    send: async (command) => {
      if (command.constructor.name === 'TransactWriteCommand') {
        f.events.push(command);
        assert.equal(command.input.TransactItems.length, 2);
        assert.ok(command.input.TransactItems.every((item) => item.Put));
        return {};
      }
      return ddb.send(command);
    },
  });
  const response = await f.route('executions.post.ts');
  assert.equal(response.requested, true);
});

test('stored READY counterexample stays inspectable but is not displayed as approvable', async () => {
  const f = fixture();
  f.book.execution_steps.push({
    step_id: 'extra',
    action: 'stop',
    success_criteria: 'done',
    commands: [
      'aws ecs stop-task --cluster cluster --task extra-task --region us-east-1',
    ],
  });
  resealRecoveryPlan(f);
  const response = await f.route('analysis-parts/[id].get.ts');
  assert.equal(response.parts[0].approval_status, 'UNAVAILABLE');
  assert.equal(response.parts[0].available, true);
  assert.deepEqual(response.parts[0].payload.result.playbook, f.book);
});
test('retransmission cannot publish an unsafe early snapshot left by the previous permissive gate', async () => {
  const f = fixture();
  f.failSend(true);
  await assert.rejects(f.route('executions.post.ts'), { statusCode: 503 });
  const reserved = f.rows.find((row) => row.SK === `EXEC#${approvalId}`);
  f.book.execution_steps.push({
    step_id: 'extra',
    action: 'stop',
    success_criteria: 'done',
    commands: [
      'aws ecs stop-task --cluster cluster --task extra-task --region us-east-1',
    ],
  });
  const raw = util.serializePlaybookSnapshot(f.book);
  reserved.playbook_digest = util.sha256Hex(raw);
  f.body.expectedPlaybookDigest = reserved.playbook_digest;
  f.objects.set(reserved.approved_playbook_s3_key, Buffer.from(raw).toString());
  f.failSend(false);
  f.events.length = 0;
  await assert.rejects(f.route('executions.post.ts'), { statusCode: 409 });
  assert.ok(
    !f.events.some((event) =>
      [
        'PutObjectCommand',
        'TransactWriteCommand',
        'SendMessageCommand',
      ].includes(event.constructor.name),
    ),
  );
});

test('early operation taxonomy and decisions match the actual portable Python validator', async () => {
  const base = deploymentBook(),
    scope = base.rollback_context.scope;
  const normal = base.rollback_context.normal.task_definition_arn;
  const region = ' --region us-east-1';
  const readCases = [
    `aws ecs describe-services --cluster ${scope.cluster_arn} --services ${scope.service_arn}`,
    `aws ecs describe-tasks --cluster ${scope.cluster_arn} --tasks arn:aws:ecs:us-east-1:123456789012:task/cluster/task`,
    `aws ecs describe-task-definition --task-definition ${normal}`,
    `aws ecs list-tasks --cluster ${scope.cluster_arn} --service-name ${scope.service_name}`,
    'aws cloudwatch list-metrics --namespace Healthcare/Sensor',
    'aws cloudwatch describe-alarms --alarm-names IngestFailures',
    `aws cloudwatch get-metric-data --metric-data-queries '[{"Id":"m1","Expression":"1"}]' --start-time 2026-09-17T00:00:00Z --end-time 2026-09-17T00:01:00Z`,
    'aws cloudwatch get-metric-statistics --namespace Healthcare/Sensor --metric-name Attempts --start-time 2026-09-17T00:00:00Z --end-time 2026-09-17T00:01:00Z --period 60 --statistics Sum',
    'aws logs filter-log-events --log-group-name /ecs/app --start-time 0 --end-time 60',
    'aws logs get-log-events --log-group-name /ecs/app --log-stream-name stream --start-time 0 --end-time 60',
  ];
  const invalidCases = [
    'aws ecs stop-task --cluster cluster --task extra',
    'aws application-autoscaling register-scalable-target --service-namespace ecs --resource-id service/cluster/app --scalable-dimension ecs:service:DesiredCount --min-capacity 1 --max-capacity 2',
    'aws logs start-query --log-group-name /ecs/app --start-time 0 --end-time 60 --query-string stats',
    'aws ecs list-tasks --cluster other --service-name app',
    'aws ecs describe-services --cluster cluster --services other',
    'aws ecs describe-task-definition --task-definition arn:aws:ecs:us-east-1:123456789012:task-definition/app:999',
    'aws logs filter-log-events --log-group-name /other --start-time 0 --end-time 60',
    'aws logs filter-log-events --log-group-name /ecs/app',
    'aws cloudwatch describe-alarms --alarm-names IngestFailures --output text',
    'aws cloudwatch describe-alarms --alarm-names IngestFailures --no-paginate',
  ];
  const cases = [
    base,
    ...[...readCases, ...invalidCases].map((command) => {
      const book = structuredClone(base);
      book.execution_steps[0].commands.push(command + region);
      return book;
    }),
    {
      ...structuredClone(base),
      execution_steps: [structuredClone(base.execution_steps[0])],
    },
  ];
  const expected = cases.map(
    (book) => util.validateEarlyRecoveryPlaybook(book).valid,
  );
  assert.deepEqual(expected, [
    true,
    ...readCases.map(() => true),
    ...invalidCases.map(() => false),
    false,
  ]);
  const python = `
import ast,inspect,json,sys
from rca_agent.services.analysis_parts import validate_recovery_operations,RECOVERY_READ_OPERATIONS
books=json.load(sys.stdin)
results=[]
for book in books:
 try:
  validate_recovery_operations(book)
  results.append(True)
 except (ValueError,TypeError,KeyError): results.append(False)
node=ast.parse(inspect.getsource(validate_recovery_operations)).body[0]
assignment=next(n for n in node.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='allowed_flags' for t in n.targets))
flags=ast.literal_eval(assignment.value)
print(json.dumps({'results':results,'operations':sorted(' '.join(op) for op in RECOVERY_READ_OPERATIONS),'flags':{' '.join(op):sorted(value) for op,value in flags.items()}}))
`;
  const result = spawnSync(
    path.join(root, 'packages/agent/.venv/bin/python'),
    ['-c', python],
    {
      cwd: root,
      env: {
        ...process.env,
        PYTHONPATH: path.join(root, 'packages/agent/src'),
        PYTHONDONTWRITEBYTECODE: '1',
        AWS_EC2_METADATA_DISABLED: 'true',
      },
      input: JSON.stringify(cases),
      encoding: 'utf8',
    },
  );
  assert.equal(result.status, 0, result.stderr);
  const portable = JSON.parse(result.stdout),
    flags = JSON.parse(
      readFileSync(
        path.join(
          root,
          'packages/dashboard/server/utils/early-recovery-read-operations.json',
        ),
        'utf8',
      ),
    );
  assert.deepEqual(portable.results, expected);
  assert.deepEqual(portable.operations, Object.keys(flags).sort());
  assert.deepEqual(portable.flags, flags);
});

test('old-engine early URL receives handoff metadata and retains old parts read-only', async () => {
  const f = fixture();
  // Keep current Headless parts, and preserve a prior Strands recovery with its own immutable payload.
  const old = structuredClone(f.recovery),
    payload = JSON.parse(f.objects.get(old.payload_s3_key));
  payload.engine = 'strands';
  const raw = JSON.stringify(payload),
    hash = sha(raw);
  Object.assign(old, {
    engine: 'strands',
    SK: 'strands#ANALYSIS_PART#recovery',
    revision: hash,
    payload_sha256: hash,
    payload_s3_key: `analysis-parts/strands/fixture/recovery/${hash}.json`,
  });
  f.objects.set(old.payload_s3_key, raw);
  f.rows.push(old);
  for (const [part, status] of [
    ['root_cause', 'RUNNING'],
    ['operations', 'WAITING'],
  ])
    f.rows.push({
      ...old,
      SK: `strands#ANALYSIS_PART#${part}`,
      part,
      status,
      revision: undefined,
      payload_s3_key: undefined,
      payload_sha256: undefined,
    });
  f.globals.getQuery = () => ({ engine: 'strands' });
  const session = await f.route('sessions/[id]/index.get.ts');
  assert.equal(session.engine, 'headless-codex');
  assert.equal(session.requestedEngine, 'strands');
  assert.equal(session.engineHandoff, true);
  const parts = await f.route('analysis-parts/[id].get.ts');
  assert.equal(parts.engine, 'strands');
  assert.equal(parts.activeEngine, 'headless-codex');
  assert.equal(parts.historicalView, true);
  assert.equal(parts.parentEligible, false);
  assert.equal(parts.parts[0].available, true);
  assert.deepEqual(
    parts.parts.map((part) => part.status),
    ['COMPLETED', 'RUNNING', 'WAITING'],
  );
  f.body.engine = 'strands';
  f.body.expectedRecoveryRevision = hash;
  await assert.rejects(f.route('executions.post.ts'), { statusCode: 409 });
  assert.ok(
    !f.events.some((event) =>
      [
        'PutObjectCommand',
        'TransactWriteCommand',
        'SendMessageCommand',
      ].includes(event.constructor.name),
    ),
  );
});

test('incident approval copy preserves exact noncanonical JSON bytes and survives analysis source removal', async () => {
  const f = fixture();
  const original = originalAlarm(f);
  original.numeric_marker = 0;
  replaceFrozenAlarm(f, original);
  const incident = f.rows[1];
  const raw = f.objects
    .get(incident.payload_s3_key)
    .replace('"numeric_marker":0', '"numeric_marker":900719925474099312345');
  const digest = sha(raw);
  incident.payload_sha256 = digest;
  incident.payload_s3_key = `analysis-parts/${incident.engine}/fixture/incident/${digest}.json`;
  f.objects.set(incident.payload_s3_key, raw);
  for (const row of f.rows.filter((row) => row.part)) {
    const payload = JSON.parse(f.objects.get(row.payload_s3_key));
    payload.incident_ref = { key: incident.payload_s3_key, sha256: digest };
    const text = JSON.stringify(payload),
      hash = sha(text);
    Object.assign(row, {
      revision: hash,
      payload_sha256: hash,
      payload_s3_key: `analysis-parts/${engine}/fixture/${row.part}/${hash}.json`,
      incident_s3_key: incident.payload_s3_key,
      incident_sha256: digest,
    });
    f.objects.set(row.payload_s3_key, text);
  }
  f.body.expectedRecoveryRevision = f.recovery.revision;
  f.failSend(true);
  await assert.rejects(f.route('executions.post.ts'), { statusCode: 503 });
  const reserved = f.rows.find((row) => row.SK === `EXEC#${approvalId}`);
  assert.equal(f.objects.get(reserved.source_incident_s3_key), raw);
  assert.equal(reserved.source_incident_sha256, digest);
  assert.equal(reserved.original_incident_s3_key, incident.payload_s3_key);
  f.objects.delete(incident.payload_s3_key);
  incident.body_expires_at = 1;
  f.failSend(false);
  f.events.length = 0;
  await f.route('executions.post.ts');
  assert.equal(f.objects.get(reserved.source_incident_s3_key), raw);
  assert.ok(
    !f.events.some(
      (event) =>
        event.constructor.name === 'GetObjectCommand' &&
        event.input.Key.startsWith('analysis-parts/'),
    ),
  );
});
function setupDeletion(f, otherEngine = false) {
  f.rows[0].state = 'COMPLETED';
  f.rows.push(priorEngineExecution());
  if (otherEngine) {
    f.rows[0].SK = 'headless-codex#SESSION';
    f.rows.push({
      PK: 'RCA#fixture',
      SK: 'ANALYSIS#SESSION',
      engine: 'strands',
      workflow: 'recovery-first-v1',
      state: 'HYPOTHESIS_GENERATION',
      ttl: 9999999999,
    });
  }
  f.globals.useDynamoDB = () => ({
    send: async (command) => {
      f.events.push(command);
      if (command.constructor.name === 'QueryCommand')
        return { Items: structuredClone(f.rows) };
      if (command.constructor.name === 'GetCommand')
        return {
          Item: structuredClone(
            f.rows.find((row) => row.SK === command.input.Key.SK),
          ),
        };
      if (command.constructor.name === 'TransactWriteCommand') {
        assert.ok(
          command.input.TransactItems.some(
            (item) =>
              item.ConditionCheck?.Key.SK === 'EXEC_ACTIVE' &&
              item.ConditionCheck.ConditionExpression ===
                'attribute_not_exists(PK)',
          ),
        );
        const update = command.input.TransactItems.find(
          (item) => item.Update,
        )?.Update;
        const row = f.rows.find((row) => row.SK === update.Key.SK);
        assert.equal(row.state, 'COMPLETED');
        row.deleting_at = 'claimed';
        return {};
      }
      if (command.constructor.name === 'BatchWriteCommand') {
        for (const requests of Object.values(command.input.RequestItems))
          for (const request of requests) {
            const index = f.rows.findIndex(
              (row) => row.SK === request.DeleteRequest.Key.SK,
            );
            if (index >= 0) f.rows.splice(index, 1);
          }
        return {};
      }
      throw new Error('unexpected deletion command');
    },
  });
  f.globals.useS3 = () => ({
    send: async (command) => {
      f.events.push(command);
      if (command.constructor.name === 'ListObjectsV2Command')
        return { Contents: [{ Key: command.input.Prefix + 'stored.json' }] };
      return {};
    },
  });
}
test('analysis deletion cleans scoped part payloads but retains other-engine incident dependencies and execution audit', async () => {
  const f = fixture();
  setupDeletion(f, true);
  await f.route('sessions/[id].delete.ts');
  assert.ok(f.rows.some((row) => row.SK === 'INCIDENT_SNAPSHOT'));
  assert.ok(f.rows.some((row) => row.SK === 'EXEC#prior-strands'));
  assert.ok(
    f.rows.some(
      (row) => row.SK === 'ANALYSIS#SESSION' && row.engine === 'strands',
    ),
  );
  const prefixes = f.events
    .filter((event) => event.constructor.name === 'ListObjectsV2Command')
    .map((event) => event.input.Prefix);
  assert.ok(
    prefixes.includes('analysis-parts/headless-codex/fixture/recovery/'),
  );
  assert.ok(
    !prefixes.some(
      (prefix) =>
        prefix.includes('/incident/') ||
        prefix.startsWith('analysis-parts/strands/') ||
        prefix.startsWith('approvals/') ||
        prefix.startsWith('executions/'),
    ),
  );
});
test('last analysis deletion removes canonical incident while keeping approval copies and execution audit', async () => {
  const f = fixture();
  setupDeletion(f);
  await f.route('sessions/[id].delete.ts');
  assert.ok(!f.rows.some((row) => row.SK === 'INCIDENT_SNAPSHOT'));
  assert.ok(f.rows.some((row) => row.SK === 'EXEC#prior-strands'));
  const prefixes = f.events
    .filter((event) => event.constructor.name === 'ListObjectsV2Command')
    .map((event) => event.input.Prefix);
  assert.ok(
    prefixes.includes('analysis-parts/headless-codex/fixture/incident/'),
  );
  assert.ok(
    !prefixes.some(
      (prefix) =>
        prefix.startsWith('approvals/') || prefix.startsWith('executions/'),
    ),
  );
});

/** Derive the GSI's real INCLUDE projection from CDK source; never hand a route full base rows as index results. */
function sessionIndexProjection() {
  const filename = path.join(
    root,
    'packages/infra/lib/stacks/database-stack.ts',
  );
  const tree = ts.createSourceFile(
    filename,
    readFileSync(filename, 'utf8'),
    ts.ScriptTarget.Latest,
    true,
  );
  let projection;
  const property = (object, name) =>
    object.properties.find((p) => p.name?.getText(tree) === name)?.initializer;
  function visit(node) {
    if (
      ts.isCallExpression(node) &&
      node.expression.getText(tree).endsWith('.addGlobalSecondaryIndex')
    ) {
      const object = node.arguments[0];
      if (
        object &&
        ts.isObjectLiteralExpression(object) &&
        property(object, 'indexName')?.text === 'session-by-engine-index'
      ) {
        assert.equal(
          property(object, 'projectionType').getText(tree),
          'dynamodb.ProjectionType.INCLUDE',
        );
        const names = property(object, 'nonKeyAttributes');
        assert.ok(ts.isArrayLiteralExpression(names));
        assert.ok(names.elements.every(ts.isStringLiteral));
        projection = new Set([
          'PK',
          'SK',
          property(property(object, 'partitionKey'), 'name').text,
          property(property(object, 'sortKey'), 'name').text,
          ...names.elements.map((n) => n.text),
        ]);
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(tree);
  assert.ok(projection);
  assert.equal(projection.has('workflow'), false);
  return projection;
}
function enforceActualSessionProjection(f, indexRows = null) {
  const projected = sessionIndexProjection();
  const select = (row, attributes) =>
    Object.fromEntries(
      Object.entries(row).filter(([key]) => attributes.has(key)),
    );
  f.globals.useDynamoDB = () => ({
    send: async (command) => {
      f.events.push(command);
      const input = command.input;
      const fields = input.ProjectionExpression?.split(',').map(
        (field) =>
          input.ExpressionAttributeNames?.[field.trim()] ?? field.trim(),
      );
      if (input.IndexName) {
        assert.equal(command.constructor.name, 'QueryCommand');
        assert.equal(input.IndexName, 'session-by-engine-index');
        assert.notEqual(input.ConsistentRead, true);
        for (const field of fields ?? [])
          if (!projected.has(field))
            throw Object.assign(new Error(`GSI does not project [${field}]`), {
              name: 'ValidationException',
            });
        const rows = (indexRows ?? f.rows)
          .filter(
            (row) =>
              row.list_engine === input.ExpressionAttributeValues[':engine'],
          )
          .sort((a, b) =>
            String(b.list_created_at).localeCompare(String(a.list_created_at)),
          );
        const offset = input.ExclusiveStartKey
          ? rows.findIndex(
              (row) =>
                row.PK === input.ExclusiveStartKey.PK &&
                row.SK === input.ExclusiveStartKey.SK,
            ) + 1
          : 0;
        const page = rows.slice(offset, offset + (input.Limit ?? 100));
        return {
          Items: page.map((row) =>
            select(select(row, projected), new Set(fields ?? projected)),
          ),
          ...(offset + page.length < rows.length
            ? {
                LastEvaluatedKey: select(
                  page.at(-1),
                  new Set(['PK', 'SK', 'list_engine', 'list_created_at']),
                ),
              }
            : {}),
        };
      }
      if (command.constructor.name === 'GetCommand') {
        assert.equal(input.ConsistentRead, true);
        const row = f.rows.find(
          (row) => row.PK === input.Key.PK && row.SK === input.Key.SK,
        );
        return {
          Item: row
            ? structuredClone(fields ? select(row, new Set(fields)) : row)
            : undefined,
        };
      }
      assert.equal(
        command.constructor.name,
        'QueryCommand',
        'preflight list/summary only read',
      );
      return {
        Items: structuredClone(
          f.rows.filter(
            (row) => row.PK === input.ExpressionAttributeValues[':pk'],
          ),
        ),
      };
    },
  });
}
test('empty summary honors actual CDK GSI projection even when the index returns no records', async () => {
  const f = fixture();
  f.rows.length = 0;
  enforceActualSessionProjection(f);
  const summary = await f.route('sessions-summary.get.ts');
  assert.equal(summary.total, 0);
  assert.deepEqual(summary.byState, {});
});
test('summary discovers active recovery from base-table workflow absent from real GSI projection', async () => {
  const f = fixture();
  Object.assign(f.rows[0], {
    list_engine: engine,
    list_created_at: '2026-09-17T01:00:00Z',
    created_at: '2026-09-17T01:00:00Z',
  });
  enforceActualSessionProjection(f);
  const summary = await f.route('sessions-summary.get.ts');
  assert.equal(summary.total, 1);
  assert.equal(summary.byReadiness.AWAITING_APPROVAL, 1);
  assert.equal(summary.completedOutcomes[0].workflow, 'recovery-first-v1');
  assert.ok(
    f.events.some(
      (event) =>
        event.constructor.name === 'GetCommand' && !event.input.IndexName,
    ),
  );
});
test('list reads fresh parent fields after indexed discovery instead of treating stale GSI metadata as current', async () => {
  const f = fixture();
  Object.assign(f.rows[0], {
    state: 'FAILED',
    root_cause: 'fresh parent result',
    list_engine: engine,
    list_created_at: '2026-09-17T01:00:00Z',
    created_at: '2026-09-17T01:00:00Z',
  });
  const stale = {
    ...f.rows[0],
    engine: 'strands',
    list_engine: 'strands',
    state: 'HYPOTHESIS_GENERATION',
    root_cause: 'stale index',
  };
  f.rows.push(priorEngineExecution());
  enforceActualSessionProjection(f, [stale]);
  const result = await f.route('sessions.get.ts');
  assert.equal(result.sessions.length, 1);
  assert.equal(result.sessions[0].engine, engine);
  assert.equal(result.sessions[0].state, 'FAILED');
  assert.equal(result.sessions[0].rootCause, 'fresh parent result');
  assert.equal(result.sessions[0].executionState, 'RESOLVED');
});
test('stale index records whose base parent was deleted do not recreate summary sessions', async () => {
  const f = fixture();
  const stale = {
    ...f.rows[0],
    list_engine: engine,
    list_created_at: '2026-09-17T01:00:00Z',
  };
  f.rows.length = 0;
  enforceActualSessionProjection(f, [stale]);
  const summary = await f.route('sessions-summary.get.ts');
  assert.equal(summary.total, 0);
  const list = await f.route('sessions.get.ts');
  assert.deepEqual(list.sessions, []);
});

test('base-table confirmed status reaches aggregate outcomes without implying recovery readiness', async () => {
  const f = fixture();
  Object.assign(f.rows[0], {
    state: 'COMPLETED',
    confirmed: true,
    list_engine: engine,
    list_created_at: '2026-09-17T01:00:00Z',
  });
  f.recovery.approval_status = 'UNAVAILABLE';
  enforceActualSessionProjection(f);
  const summary = await f.route('sessions-summary.get.ts');
  assert.equal(summary.completedOutcomes[0].confirmed, true);
  assert.equal(summary.completedOutcomes[0].readiness, 'NO_PROCEDURE');
  const { countSessionOutcomes } = load(
    'packages/dashboard/app/utils/sessionState.ts',
  );
  const counts = countSessionOutcomes(
    summary.byState,
    summary.completedOutcomes,
  );
  assert.equal(counts.get('NO_PROCEDURE'), 1);
  assert.equal(counts.has('NO_CAUSE'), false);
});

function reportReadFixture(overrides = {}) {
  const item = {
    engine: 'strands',
    state: 'FAILED',
    workflow: 'recovery-first-v1',
    analysis_parts_finalized: true,
    report_s3_key: 'reports/final-failed.md',
    root_cause: 'observed root',
    confirmed: false,
    ...overrides,
  };
  const calls = [];
  const globals = {
    ...util,
    ...load('packages/dashboard/server/utils/reportSummary.ts'),
    defineEventHandler: (fn) => fn,
    getRouterParam: () => 'failed-rca',
    getQuery: () => ({ engine: 'strands' }),
    useRuntimeConfig: () => ({
      dynamodbTableName: 'table',
      s3ReportBucket: 'reports',
    }),
    createError: (spec) => Object.assign(new Error(spec.statusMessage), spec),
    useDynamoDB: () => ({
      send: async (command) => {
        calls.push(command);
        assert.equal(command.constructor.name, 'GetCommand');
        assert.equal(command.input.Key.PK, 'RCA#failed-rca');
        if (command.input.Key.SK !== 'ANALYSIS#SESSION') return {};
        const fields = command.input.ProjectionExpression.split(',').map(
          (field) =>
            command.input.ExpressionAttributeNames?.[field.trim()] ??
            field.trim(),
        );
        return {
          Item: Object.fromEntries(
            Object.entries(item).filter(([key]) => fields.includes(key)),
          ),
        };
      },
    }),
    useS3: () => ({
      send: async (command) => {
        calls.push(command);
        assert.equal(command.constructor.name, 'GetObjectCommand');
        if (overrides.missingObject)
          throw Object.assign(new Error('missing'), { name: 'NoSuchKey' });
        return {
          Body: {
            transformToString: async () =>
              '# Retained failed report\n\nActual recorded partial results',
          },
        };
      },
    }),
  };
  return {
    item,
    calls,
    run: () =>
      load(
        'packages/dashboard/server/api/reports/[id].get.ts',
        globals,
      ).default({}),
  };
}
test('actual report route reads persisted finalized FAILED analysis without changing state or granting completion', async () => {
  const f = reportReadFixture();
  const before = structuredClone(f.item);
  const report = await f.run();
  assert.match(report.markdown, /Actual recorded partial results/);
  assert.equal(report.summary.confirmed, false);
  assert.deepEqual(f.item, before);
  const reads = f.calls.filter(
    (c) => c.constructor.name === 'GetObjectCommand',
  );
  assert.equal(reads.length, 1);
  assert.equal(reads[0].input.Key, 'reports/final-failed.md');
});
for (const [name, override] of [
  ['not finalized', { analysis_parts_finalized: false }],
  ['string flag', { analysis_parts_finalized: 'true' }],
  ['missing key', { report_s3_key: '' }],
  ['cancelled', { state: 'CANCELLED' }],
  ['outdated', { state: 'OUTDATED' }],
  ['running', { state: 'REPORT_GENERATION' }],
  ['legacy failed', { workflow: undefined }],
])
  test(`failed-report read exception rejects ${name}`, async () => {
    const f = reportReadFixture(override);
    await assert.rejects(f.run(), { statusCode: 404 });
    assert.equal(
      f.calls.filter((c) => c.constructor.name === 'GetObjectCommand').length,
      0,
    );
  });
test('missing finalized failure body never falls back to an inferred report key', async () => {
  const f = reportReadFixture({ missingObject: true });
  await assert.rejects(f.run(), { statusCode: 404 });
  assert.deepEqual(
    f.calls
      .filter((c) => c.constructor.name === 'GetObjectCommand')
      .map((c) => c.input.Key),
    ['reports/final-failed.md'],
  );
});
