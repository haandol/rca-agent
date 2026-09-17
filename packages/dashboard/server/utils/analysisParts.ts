import {
  QueryCommand,
  type QueryCommandInput,
  type TransactWriteCommandInput,
} from '@aws-sdk/lib-dynamodb';
import { createHash } from 'node:crypto';
import { GetObjectCommand } from '@aws-sdk/client-s3';
import { validateEarlyRecoveryPlaybook } from './playbook.ts';
import { serializePlaybookSnapshot, sha256Hex } from './executionApproval.ts';

export const ANALYSIS_WORKFLOW = 'recovery-first-v1';
export const ANALYSIS_PARTS = ['recovery', 'root_cause', 'operations'] as const;
type Row = Record<string, any>;
type Reader = { send(command: any): Promise<any> };
const hashPattern = /^[a-f0-9]{64}$/;
const liveExpiry = (value: unknown): boolean =>
  typeof value === 'number' &&
  Number.isFinite(value) &&
  value > Date.now() / 1000;
const states = ['WAITING', 'RUNNING', 'COMPLETED', 'FAILED', 'SKIPPED'];

/** Bind every object to stored identity and exact bytes; a caller never selects a bucket or key. */
async function analysisObject(
  s3: Reader,
  bucket: string,
  key: unknown,
  hash: unknown,
  id: string,
  engine: string,
  part: string,
  captureBytes?: (bytes: Uint8Array) => void,
): Promise<Row> {
  if (
    !bucket ||
    typeof hash !== 'string' ||
    !hashPattern.test(hash) ||
    key !== `analysis-parts/${engine}/${id}/${part}/${hash}.json`
  )
    throw new Error('분석 원본 위치 또는 지문이 일치하지 않습니다');
  const response = await s3.send(
    new GetObjectCommand({ Bucket: bucket, Key: key as string }),
  );
  const bytes = await response.Body?.transformToByteArray();
  if (!bytes || createHash('sha256').update(bytes).digest('hex') !== hash)
    throw new Error('분석 원본 내용 지문이 일치하지 않습니다');
  const payload = JSON.parse(
    new TextDecoder('utf-8', { fatal: true }).decode(bytes),
  );
  if (
    !payload ||
    Array.isArray(payload) ||
    payload.schema_version !== 1 ||
    payload.rca_id !== id ||
    payload.engine !== engine
  )
    throw new Error('분석 원본 식별자가 일치하지 않습니다');
  captureBytes?.(bytes);
  return payload;
}

/** Cancellation blocks new approvals, not historical reads or an already reserved execution. */
export function analysisParentEligible(
  parent: Row | undefined,
  id: string,
  engine: string,
): boolean {
  return Boolean(
    parent &&
    parent.PK === `RCA#${id}` &&
    parent.SK === 'ANALYSIS#SESSION' &&
    parent.engine === engine &&
    (!Object.hasOwn(parent, 'rca_id') || parent.rca_id === id) &&
    parent.workflow === ANALYSIS_WORKFLOW &&
    !Object.hasOwn(parent, 'deleting_at') &&
    typeof parent.state === 'string' &&
    parent.state.length > 0 &&
    !['CANCELLED', 'OUTDATED'].includes(parent.state) &&
    liveExpiry(parent.ttl),
  );
}

export interface RecoveryAlarmSource {
  name: string;
  incidentKey: string;
  incidentHash: string;
  incidentEngine: string;
  bytes: Uint8Array;
}

/** The full original alarm remains in immutable S3; approval stores only its canonical source reference. */
export function recoveryAlarmSource(
  frozen: Row,
  parent: Row,
  book: Row,
  incident: Row,
  bytes: Uint8Array,
): RecoveryAlarmSource {
  const original = frozen.alarm;
  if (
    !original ||
    typeof original !== 'object' ||
    Array.isArray(original) ||
    typeof original.AlarmName !== 'string'
  )
    throw new Error('고정 incident에 전체 원본 알람이 없습니다');
  const name = original.AlarmName;
  const scope = book.rollback_context?.scope;
  const waits = (book.execution_steps ?? [])
    .filter((step: Row) => step.metric_wait)
    .map((step: Row) => step.metric_wait);
  if (
    typeof name !== 'string' ||
    !name.trim() ||
    (typeof parent.alarm_name === 'string' && parent.alarm_name !== name) ||
    !waits.length ||
    waits.some((wait: Row) => wait.failure_alarm_name !== name)
  )
    throw new Error('고정 원본 실패 알람과 런북이 일치하지 않습니다');
  const arn =
    typeof original.AlarmArn === 'string' ? original.AlarmArn.split(':') : [];
  if (
    !scope ||
    arn[2] !== 'cloudwatch' ||
    arn[3] !== scope.region ||
    arn[4] !== scope.account_id ||
    arn.slice(5).join(':') !== `alarm:${name}` ||
    (original.AWSAccountId != null &&
      original.AWSAccountId !== scope.account_id)
  )
    throw new Error('고정 원본 알람의 계정·리전·대상이 런북과 다릅니다');
  return {
    name,
    incidentKey: incident.payload_s3_key,
    incidentHash: incident.payload_sha256,
    incidentEngine: incident.engine,
    bytes,
  };
}

/** Isolate each unavailable body so one missing or invalid part cannot hide its siblings. */
export async function readAnalysisParts(
  items: Row[],
  id: string,
  engine: string,
  s3: Reader,
  bucket: string,
  requestedParts: readonly string[] = ANALYSIS_PARTS,
  includeRecoveryAuthority = false,
) {
  if (
    !/^[A-Za-z0-9_-]+$/.test(id) ||
    !['strands', 'headless-codex'].includes(engine)
  )
    throw new Error('Invalid analysis identity');
  const parent = items.find((row) => row.SK === 'ANALYSIS#SESSION');
  const parentMatches =
    parent?.PK === `RCA#${id}` &&
    ['strands', 'headless-codex'].includes(parent.engine) &&
    (!Object.hasOwn(parent, 'rca_id') || parent.rca_id === id);
  const incident = items.find((row) => row.SK === 'INCIDENT_SNAPSHOT');
  let frozen: Promise<Row> | undefined;
  let incidentBytes: Uint8Array | undefined;
  const readIncident = () =>
    (frozen ??= (async () => {
      if (
        !incident ||
        incident.PK !== `RCA#${id}` ||
        incident.rca_id !== id ||
        !['strands', 'headless-codex'].includes(incident.engine) ||
        incident.workflow !== ANALYSIS_WORKFLOW ||
        incident.schema_version !== 1 ||
        !liveExpiry(incident.body_expires_at) ||
        !liveExpiry(incident.ttl)
      )
        throw new Error('고정 장애 원본이 없거나 만료되었습니다');
      return analysisObject(
        s3,
        bucket,
        incident.payload_s3_key,
        incident.payload_sha256,
        id,
        incident.engine,
        'incident',
        (bytes) => {
          incidentBytes = bytes;
        },
      );
    })());
  let recoveryAlarm: RecoveryAlarmSource | undefined;
  const parts = await Promise.all(
    requestedParts.map(async (part) => {
      const record = items.find(
        (row) => row.SK === `${engine}#ANALYSIS_PART#${part}`,
      );
      const view = {
        part,
        status:
          record && states.includes(record.status)
            ? (record.status as string)
            : 'WAITING',
        revision: '',
        summary: '',
        error: '',
        approval_status: 'UNAVAILABLE',
        runbook_digest: '',
        available: false,
        payload: null as Row | null,
      };
      if (!record) return view;
      try {
        if (
          !parentMatches ||
          record.PK !== `RCA#${id}` ||
          record.rca_id !== id ||
          record.engine !== engine ||
          record.part !== part ||
          record.workflow !== ANALYSIS_WORKFLOW ||
          record.schema_version !== 1 ||
          !states.includes(record.status)
        )
          throw new Error('분석 파트 식별자가 일치하지 않습니다');
        view.summary = typeof record.summary === 'string' ? record.summary : '';
        view.error = typeof record.error === 'string' ? record.error : '';
        view.approval_status = ['READY', 'UNAVAILABLE', 'REVOKED'].includes(
          record.approval_status,
        )
          ? record.approval_status
          : 'UNAVAILABLE';
        if (!liveExpiry(record.body_expires_at) || !liveExpiry(record.ttl))
          throw new Error('분석 원본 보존 기간이 지났습니다');
        if (!record.revision && ['WAITING', 'RUNNING'].includes(record.status))
          return view;
        if (record.revision !== record.payload_sha256)
          throw new Error('분석 개정본이 일치하지 않습니다');
        const frozenInput = await readIncident();
        if (
          part === 'recovery' &&
          (frozenInput.alarm?.eval_source_metadata != null ||
            frozenInput.scoping?.raw_alarm?.eval_source_metadata != null)
        )
          view.approval_status = 'UNAVAILABLE';
        if (
          record.incident_s3_key !== incident!.payload_s3_key ||
          record.incident_sha256 !== incident!.payload_sha256
        )
          throw new Error('장애 원본 참조가 일치하지 않습니다');
        const payload = await analysisObject(
          s3,
          bucket,
          record.payload_s3_key,
          record.payload_sha256,
          id,
          engine,
          part,
        );
        if (
          payload.workflow !== ANALYSIS_WORKFLOW ||
          payload.part !== part ||
          payload.status !== record.status ||
          !Array.isArray(payload.input_refs) ||
          !Array.isArray(payload.limitations) ||
          payload.incident_ref?.key !== record.incident_s3_key ||
          payload.incident_ref?.sha256 !== record.incident_sha256 ||
          !payload.result ||
          typeof payload.result !== 'object' ||
          Array.isArray(payload.result)
        )
          throw new Error('분석 파트 내용이 권위 기록과 다릅니다');
        if (includeRecoveryAuthority && part === 'recovery')
          recoveryAlarm = recoveryAlarmSource(
            frozenInput,
            parent!,
            payload.result.playbook ?? {},
            incident!,
            incidentBytes!,
          );
        if (part === 'recovery' && view.approval_status === 'READY') {
          const earlyValidation = validateEarlyRecoveryPlaybook(
            payload.result.playbook ?? null,
          );
          if (!earlyValidation.valid) {
            view.approval_status = 'UNAVAILABLE';
            view.error = earlyValidation.reason;
          }
        }
        view.revision = record.revision;
        view.runbook_digest =
          typeof record.runbook_digest === 'string'
            ? record.runbook_digest
            : '';
        view.payload = payload;
        view.available = true;
      } catch (error) {
        view.error =
          error instanceof Error && /^(분석|고정|장애) /.test(error.message)
            ? error.message
            : '분석 원본을 읽을 수 없습니다';
        view.approval_status = 'UNAVAILABLE';
      }
      return view;
    }),
  );
  return {
    rcaId: id,
    engine,
    workflow: ANALYSIS_WORKFLOW,
    activeEngine: parentMatches ? String(parent.engine) : '',
    historicalView: Boolean(parentMatches && parent.engine !== engine),
    parentEligible: analysisParentEligible(parent, id, engine),
    ...(includeRecoveryAuthority ? { recoveryAlarm } : {}),
    parts,
  };
}

/** READY metadata is necessary but never substitutes for the complete stored rollback contract. */
export async function readReadyRecovery(
  items: Row[],
  id: string,
  engine: string,
  s3: Reader,
  bucket: string,
) {
  const response = await readAnalysisParts(
    items,
    id,
    engine,
    s3,
    bucket,
    ['recovery'],
    true,
  );
  const recovery = response.parts[0]!;
  const book = recovery.payload?.result?.playbook;
  const validation = validateEarlyRecoveryPlaybook(book ?? null);
  if (
    !response.parentEligible ||
    !response.recoveryAlarm ||
    recovery.status !== 'COMPLETED' ||
    recovery.approval_status !== 'READY' ||
    !recovery.available ||
    recovery.payload?.result?.recommendation !== 'ROLLBACK' ||
    recovery.payload?.result?.verification?.valid !== true ||
    !book?.rollback_context ||
    !validation.valid ||
    !validation.steps.some((step) => step.deployment_wait) ||
    sha256Hex(serializePlaybookSnapshot(book)) !== recovery.runbook_digest
  )
    throw new Error(recovery.error || '현재 정상화 런북은 승인할 수 없습니다');
  return {
    playbook: book as Row,
    sourceAlarm: response.recoveryAlarm!,
    record: items.find((row) => row.SK === `${engine}#ANALYSIS_PART#recovery`)!,
    revision: recovery.revision,
  };
}

/** Check the same still-READY revision and parent inside the execution reservation transaction. */
export function recoveryReservationChecks(
  table: string,
  record: Row,
  alarmSource: RecoveryAlarmSource,
): NonNullable<TransactWriteCommandInput['TransactItems']> {
  const now = Math.floor(Date.now() / 1000);
  return [
    {
      ConditionCheck: {
        TableName: table,
        Key: { PK: record.PK, SK: record.SK },
        ConditionExpression:
          'revision = :revision AND payload_sha256 = :hash AND payload_s3_key = :key AND runbook_digest = :digest AND #status = :completed AND approval_status = :ready AND body_expires_at > :now AND #ttl > :now AND rca_id = :id AND engine = :engine AND workflow = :workflow',
        ExpressionAttributeNames: { '#status': 'status', '#ttl': 'ttl' },
        ExpressionAttributeValues: {
          ':revision': record.revision,
          ':hash': record.payload_sha256,
          ':key': record.payload_s3_key,
          ':digest': record.runbook_digest,
          ':completed': 'COMPLETED',
          ':ready': 'READY',
          ':now': now,
          ':id': record.rca_id,
          ':engine': record.engine,
          ':workflow': ANALYSIS_WORKFLOW,
        },
      },
    },
    {
      ConditionCheck: {
        TableName: table,
        Key: { PK: record.PK, SK: 'ANALYSIS#SESSION' },
        ConditionExpression:
          'engine = :engine AND workflow = :workflow AND (attribute_not_exists(rca_id) OR rca_id = :id) AND attribute_not_exists(deleting_at) AND attribute_exists(#state) AND NOT #state IN (:cancelled, :outdated) AND #ttl > :now',
        ExpressionAttributeNames: { '#state': 'state', '#ttl': 'ttl' },
        ExpressionAttributeValues: {
          ':engine': record.engine,
          ':workflow': ANALYSIS_WORKFLOW,
          ':id': record.rca_id,
          ':cancelled': 'CANCELLED',
          ':outdated': 'OUTDATED',
          ':now': now,
        },
      },
    },
    {
      ConditionCheck: {
        TableName: table,
        Key: { PK: record.PK, SK: 'INCIDENT_SNAPSHOT' },
        ConditionExpression:
          'payload_s3_key = :key AND payload_sha256 = :hash AND engine = :origin AND rca_id = :id AND workflow = :workflow AND schema_version = :schema AND body_expires_at > :now AND #ttl > :now',
        ExpressionAttributeNames: { '#ttl': 'ttl' },
        ExpressionAttributeValues: {
          ':key': alarmSource.incidentKey,
          ':hash': alarmSource.incidentHash,
          ':origin': alarmSource.incidentEngine,
          ':id': record.rca_id,
          ':workflow': ANALYSIS_WORKFLOW,
          ':schema': 1,
          ':now': now,
        },
      },
    },
  ];
}

/** List readiness uses the same verified source as approval, without pretending analysis completed. */
export async function recoveryReadiness(
  items: Row[],
  id: string,
  engine: string,
  s3: Reader,
  bucket: string,
) {
  try {
    const recovery = await readReadyRecovery(items, id, engine, s3, bucket);
    return {
      stepCount: recovery.playbook.execution_steps.length as number,
      ready: true,
    };
  } catch {
    return { stepCount: 0, ready: false };
  }
}

/** Read a complete incident partition before deriving readiness; traces cannot hide later part records. */
export async function readAnalysisPartition(
  ddb: Reader,
  table: string,
  id: string,
): Promise<Row[]> {
  const items: Row[] = [];
  let cursor: QueryCommandInput['ExclusiveStartKey'];
  do {
    const response = await ddb.send(
      new QueryCommand({
        TableName: table,
        KeyConditionExpression: 'PK = :pk',
        ExpressionAttributeValues: { ':pk': `RCA#${id}` },
        ConsistentRead: true,
        ExclusiveStartKey: cursor,
      }),
    );
    items.push(...(response.Items ?? []));
    cursor = response.LastEvaluatedKey;
  } while (cursor);
  return items;
}
