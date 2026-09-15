import {
  GetCommand,
  QueryCommand,
  TransactWriteCommand,
  type TransactWriteCommandInput,
} from '@aws-sdk/lib-dynamodb';
import {
  GetVectorsCommand,
  ListVectorsCommand,
  PutVectorsCommand,
  DeleteVectorsCommand,
} from '@aws-sdk/client-s3vectors';
import { InvokeModelCommand } from '@aws-sdk/client-bedrock-runtime';
import { isDeepStrictEqual } from 'node:util';
import {
  isAllowedEngine,
  rcaPk,
  playbookRevisionSk,
  ALLOWED_ENGINES,
  ANALYSIS_SESSION_SK,
  isSessionSortKey,
  parseEngine,
} from './keys';
import { findSessionForEngine, resolveCurrentPlaybook } from './playbook';
import type {
  LibraryDetail,
  LibraryItem,
  LibraryPage,
  PlaybookComparison,
  PlaybookProposal,
  ProposalResponse,
} from '../../shared/types/playbook-library';

type Row = Record<string, any>;
type Client = { send(command: any): Promise<any> };
type Transaction = NonNullable<TransactWriteCommandInput['TransactItems']>;
const ORIGINAL_RETENTION = 60 * 86400;
const STATE_RETENTION = 90 * 86400;
const LIBRARY_STATE = 'PLAYBOOK_LIBRARY_STATE';
export interface LibraryDependencies {
  ddb: Client;
  vectors: Client;
  embedding: Client;
  config: {
    dynamodbTableName: string;
    s3VectorBucketName: string;
    s3VectorPlaybookIndex: string;
    bedrockEmbeddingModelId: string;
  };
  now?: () => number;
}
const AUXILIARY = new Set([
  'comparison',
  'library_revision',
  'source_engine',
  'source_rca_id',
  'stage',
  'summary',
  'output_summary',
]);
export const KNOWLEDGE_FIELDS = new Set([
  'failure_type',
  'symptom_pattern',
  'severity_criteria',
  'verification_steps',
  'temporary_mitigation',
  'permanent_remediation',
  'escalation_criteria',
  'prevention_measures',
  'related_metrics',
  'tags',
]);

/**
 * Compare the complete historical domain without lookup or presentation fields.
 * Unknown domain fields and the entire runbook remain part of the baseline.
 */
export function domainPlaybook(value: Row): Row {
  return Object.fromEntries(
    Object.entries(value).filter(([key]) => !AUXILIARY.has(key)),
  );
}
/**
 * Check publication identity without request-specific source annotations.
 * Unlike a knowledge baseline, the published payload retains its comparison.
 */
function publishedPayload(value: Row): Row {
  return Object.fromEntries(
    Object.entries(value).filter(
      ([key]) =>
        !['library_revision', 'source_engine', 'source_rca_id'].includes(key),
    ),
  );
}
/**
 * Keep dashboard publication in the Python writers' embedding space.
 * Match their label order, 80-code-point truncation and omission of empty fields.
 */
export function libraryEmbedKey(book: Row, metricName: string): string {
  return [
    ['장애유형', book.failure_type],
    ['증상', book.symptom_pattern],
    ['메트릭', metricName],
  ]
    .flatMap(([label, value]) => {
      const text =
        typeof value === 'string'
          ? [...value].slice(0, 80).join('').trim()
          : '';
      return text ? [`${label}: ${text}`] : [];
    })
    .join(' | ');
}
/**
 * Read both stored JSON strings and legacy maps without fabricating a document.
 * Malformed JSON, scalars and arrays remain unavailable as null.
 */
function object(value: unknown): Row | null {
  if (typeof value === 'string') {
    try {
      return object(JSON.parse(value));
    } catch {
      return null;
    }
  }
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Row)
    : null;
}
/** Avoid inventing readable fields by coercion; non-string values stay absent. */
function text(value: unknown): string {
  return typeof value === 'string' ? value : '';
}
/** Keep readable list entries while excluding malformed values from API labels. */
function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((v): v is string => typeof v === 'string')
    : [];
}
/**
 * Prevent a malformed or misidentified record from becoming usable knowledge.
 * Validate stored field shapes while allowing historical prose-only runbooks.
 */
function libraryBody(value: unknown, id: string): Row | null {
  const book = object(value);
  if (
    !book ||
    book.playbook_id !== id ||
    typeof book.failure_type !== 'string' ||
    typeof book.symptom_pattern !== 'string' ||
    !['DRAFT', 'VERIFIED'].includes(book.verification_status ?? 'DRAFT')
  )
    return null;
  for (const field of [
    'tags',
    'verification_steps',
    'prevention_measures',
    'related_metrics',
  ]) {
    if (
      field in book &&
      (!Array.isArray(book[field]) ||
        book[field].some((entry: unknown) => typeof entry !== 'string'))
    )
      return null;
  }
  for (const field of [
    'severity_criteria',
    'temporary_mitigation',
    'permanent_remediation',
    'escalation_criteria',
  ]) {
    if (field in book && typeof book[field] !== 'string') return null;
  }
  if (
    'execution_steps' in book &&
    (!Array.isArray(book.execution_steps) ||
      book.execution_steps.some((entry: unknown) => {
        const step = object(entry);
        return (
          !step ||
          !text(step.step_id).trim() ||
          ('commands' in step &&
            (!Array.isArray(step.commands) ||
              step.commands.some(
                (command: unknown) => typeof command !== 'string',
              ))) ||
          (step.metric_wait != null && !object(step.metric_wait))
        );
      }))
  )
    return null;
  return book;
}
/** Give callers a stable refusal code and readable reason instead of a false success. */
export function libraryError(
  statusCode: number,
  code: string,
  message: string,
): never {
  throw Object.assign(new Error(message), {
    statusCode,
    statusMessage: message,
    data: { code },
  });
}
/** Reject malformed keys and unsupported engines before selecting storage records. */
function identity(id: string, engine?: string) {
  if (
    !id ||
    id.length > 512 ||
    /[\x00-\x1f]/.test(id) ||
    (engine !== undefined && !isAllowedEngine(engine))
  )
    libraryError(
      400,
      'INVALID_IDENTITY',
      '식별자 또는 엔진이 올바르지 않습니다.',
    );
}

/**
 * Fence changes made after a baseline read by comparing its raw attributes.
 * Explicitly absent fields must still be absent when the transaction commits.
 */
function condition(item: Row, absent: string[] = []) {
  const names: Row = {};
  const values: Row = {};
  const parts = Object.entries(item)
    .filter(([key]) => key !== 'PK' && key !== 'SK')
    .map(([key, value], index) => {
      names[`#c${index}`] = key;
      values[`:c${index}`] = value;
      return `#c${index} = :c${index}`;
    });
  for (const key of absent.filter((key) => !(key in item))) {
    const alias = `#c${Object.keys(names).length}`;
    names[alias] = key;
    parts.push(`attribute_not_exists(${alias})`);
  }
  return {
    ConditionExpression: parts.length
      ? parts.join(' AND ')
      : 'attribute_not_exists(PK)',
    ...(Object.keys(names).length ? { ExpressionAttributeNames: names } : {}),
    ...(Object.keys(values).length
      ? { ExpressionAttributeValues: values }
      : {}),
  };
}

/**
 * Share the library's read and decision rules between HTTP handlers and AWS fakes.
 * Construction has no writes; only an explicit proposal action can mutate data.
 */
export function createPlaybookLibrary(deps: LibraryDependencies) {
  const { ddb, vectors, embedding, config } = deps;
  const table = config.dynamodbTableName;
  /** Use one injectable seconds clock for retention checks and new record expiry. */
  const now = () => Math.floor((deps.now?.() ?? Date.now()) / 1000);
  /** Refuse missing index configuration rather than reading or publishing elsewhere. */
  const vectorIndex = () => {
    if (!config.s3VectorBucketName)
      libraryError(
        503,
        'VECTOR_NOT_CONFIGURED',
        'S3_VECTOR_BUCKET_NAME 설정이 필요합니다.',
      );
    return {
      vectorBucketName: config.s3VectorBucketName,
      indexName: config.s3VectorPlaybookIndex,
    };
  };
  /**
   * Enforce expiry before asynchronous DynamoDB deletion catches up.
   * Required head/session TTLs cannot use the legacy optional-TTL allowance.
   */
  const retained = (row: Row, required = false) =>
    row.ttl === undefined ? !required : Number(row.ttl) > now();
  /** Read the producer's ISO or epoch timestamp without guessing a timezone or rebasing an invalid birth. */
  function originalEpoch(value: unknown): number {
    if (typeof value === 'number' && Number.isFinite(value))
      return Math.floor(value);
    if (typeof value !== 'string') return NaN;
    if (/^\d+(?:\.\d+)?$/.test(value)) return Math.floor(Number(value));
    if (!/(?:Z|[+-]\d{2}:?\d{2})$/i.test(value)) return NaN;
    return Math.floor(Date.parse(value) / 1000);
  }
  /** Bound full original reads independently of the longer-lived summary state. */
  function bodyDeadline(row: Row): number {
    const births = ['original_created_at', 'completed_at', 'created_at']
      .filter((field) => field in row)
      .map((field) => originalEpoch(row[field]));
    if (!births.length && 'updated_at' in row)
      births.push(originalEpoch(row.updated_at));
    if (births.some((birth) => !Number.isFinite(birth) || birth > now()))
      return 0;
    const timed = births.length
      ? Math.min(...births) + ORIGINAL_RETENTION
      : Infinity;
    return Math.min(
      Number(row.ttl) || 0,
      Number(row.original_expires_at) || Infinity,
      timed,
    );
  }
  /** Date legacy originals from their selected body record, never from the age of source authority alone. */
  function legacyOriginalDeadline(record: Row, session: Row): number {
    const fields = text(record.SK).endsWith('#PLAYBOOK_REVISION')
      ? ['original_created_at', 'updated_at', 'created_at', 'completed_at']
      : ['original_created_at', 'completed_at', 'created_at', 'updated_at'];
    for (const field of fields) {
      if (!(field in record)) continue;
      const origin = originalEpoch(record[field]);
      return Number.isFinite(origin) && origin <= now()
        ? origin + ORIGINAL_RETENTION
        : 0;
    }
    if ('ttl' in record)
      return (Number(record.ttl) || 0) - (STATE_RETENTION - ORIGINAL_RETENTION);
    return record !== session ? legacyOriginalDeadline(session, session) : 0;
  }
  /** Read the current authority consistently so cached absence cannot bypass a head or decision. */
  async function get(PK: string, SK: string): Promise<Row | null> {
    return (
      (
        await ddb.send(
          new GetCommand({
            TableName: table,
            Key: { PK, SK },
            ConsistentRead: true,
          }),
        )
      ).Item ?? null
    );
  }
  /**
   * Recover ownership for old vectors that omitted an engine.
   * Prefer the neutral session; otherwise require one retained, completed legacy match.
   */
  async function legacyEngine(rcaId: string, id: string): Promise<string> {
    if (!rcaId) return '';
    const neutral = await get(rcaPk(rcaId), ANALYSIS_SESSION_SK);
    /** Admit only completed matching sources, never an engine supplied by a lookup hint. */
    const valid = (session: Row | null) =>
      session &&
      session.state === 'COMPLETED' &&
      retained(session, true) &&
      !('deleting_at' in session) &&
      session.playbook_id === id &&
      isAllowedEngine(text(session.engine) || parseEngine(text(session.SK)));
    if (neutral) return valid(neutral) ? text(neutral.engine) : '';
    const candidates = await Promise.all(
      ['SESSION', ...ALLOWED_ENGINES.map((engine) => `${engine}#SESSION`)].map(
        (SK) => get(rcaPk(rcaId), SK),
      ),
    );
    const matching = candidates.filter(valid) as Row[];
    return matching.length === 1
      ? text(matching[0]!.engine) || parseEngine(matching[0]!.SK)
      : '';
  }
  /**
   * Resolve a retained completed source and capture the raw records needed to fence it.
   * Original comparison reads survive a broken later runbook; current knowledge reads fail closed.
   */
  async function source(rcaId: string, engine: string, originalOnly = false) {
    identity(rcaId, engine);
    const items: Row[] = [];
    let next;
    do {
      const page = await ddb.send(
        new QueryCommand({
          TableName: table,
          ConsistentRead: true,
          KeyConditionExpression: 'PK = :pk',
          ExpressionAttributeValues: { ':pk': rcaPk(rcaId) },
          ExclusiveStartKey: next,
        }),
      );
      items.push(...(page.Items ?? []));
      next = page.LastEvaluatedKey;
    } while (next);
    const neutral = items.filter((item) => item.SK === ANALYSIS_SESSION_SK);
    const sessions = neutral.length
      ? neutral
      : items.filter(
          (item) =>
            isSessionSortKey(item.SK) &&
            (text(item.engine) || parseEngine(item.SK)) === engine,
        );
    const session =
      sessions.length === 1 ? findSessionForEngine(sessions, engine) : null;
    if (
      !session ||
      session.state !== 'COMPLETED' ||
      !retained(session, true) ||
      'deleting_at' in session
    )
      libraryError(
        409,
        'SOURCE_UNAVAILABLE',
        '완료된 원본 분석이 없거나 보존 기간이 지났습니다.',
      );
    const knownRevision = items.find(
      (item) => item.SK === playbookRevisionSk(engine),
    );
    if (!originalOnly && knownRevision) {
      const revisedBook = libraryBody(
        knownRevision.playbook,
        text(session.playbook_id),
      );
      if (
        !retained(knownRevision) ||
        !revisedBook ||
        knownRevision.publication_status !== 'PUBLISHED'
      )
        libraryError(
          409,
          'SOURCE_UNAVAILABLE',
          '현재 개정본이 만료되었거나 형식이 올바르지 않습니다.',
        );
    }
    const completion = object(session.completion_playbook);
    const original =
      'completion_playbook' in session
        ? completion && completion.playbook_id === session.playbook_id
          ? { playbook: completion, sourceItem: session }
          : null
        : resolveCurrentPlaybook(
            items.filter((item) => item.SK !== playbookRevisionSk(engine)),
            session,
            engine,
          );
    const resolved =
      originalOnly || !knownRevision
        ? original
        : resolveCurrentPlaybook(items, session, engine);
    if (!resolved || !retained(resolved.sourceItem))
      libraryError(
        409,
        'SOURCE_UNAVAILABLE',
        '현재 플레이북 원문을 읽을 수 없습니다.',
      );
    if (
      !originalOnly &&
      !libraryBody(resolved.playbook, text(session.playbook_id))
    )
      libraryError(
        409,
        'SOURCE_UNAVAILABLE',
        '완료 원본의 게시 상태 또는 플레이북 형식이 올바르지 않습니다.',
      );
    const guards: Row[] = [session];
    if (resolved.sourceItem.SK !== session.SK) guards.push(resolved.sourceItem);
    if (original && !guards.some((row) => row.SK === original.sourceItem.SK))
      guards.push(original.sourceItem);
    // A newly committed retrospective must invalidate a legacy baseline too.
    const revision = knownRevision;
    if (revision && !guards.some((row) => row.SK === revision.SK))
      guards.push(revision);
    return {
      session,
      book: resolved.playbook,
      originalBook: original?.playbook,
      sourceItem: resolved.sourceItem,
      guards,
      absentRevision: revision
        ? null
        : { PK: rcaPk(rcaId), SK: playbookRevisionSk(engine) },
    };
  }
  /**
   * Protect every source participating in a decision with one condition per key.
   * A revision absent at read time must not appear before commit.
   */
  function checks(sources: Awaited<ReturnType<typeof source>>[]): Transaction {
    const guards = new Map<string, Row>();
    for (const src of sources) {
      for (const row of src.guards) guards.set(`${row.PK}\0${row.SK}`, row);
      if (src.absentRevision)
        guards.set(
          `${src.absentRevision.PK}\0${src.absentRevision.SK}`,
          src.absentRevision,
        );
    }
    return [...guards.values()].map((row) => {
      const captured =
        Object.keys(row).length === 2
          ? { ConditionExpression: 'attribute_not_exists(PK)' }
          : condition(row, [
              'playbook',
              'completion_playbook',
              'playbook_id',
              'playbook_span_id',
              'ttl',
            ]);
      if (isSessionSortKey(row.SK))
        captured.ConditionExpression +=
          ' AND attribute_not_exists(deleting_at)';
      return {
        ConditionCheck: {
          TableName: table,
          Key: { PK: row.PK, SK: row.SK },
          ...captured,
        },
      };
    });
  }
  /**
   * Preserve list identity and the reason a record cannot be used.
   * Expose the annotated body only when the source and publication checks passed.
   */
  function describe(
    id: string,
    head: Row | null,
    book: Row | null,
    reason: string | null,
  ): LibraryDetail {
    const revision = text(head?.revision) || 'legacy';
    const rcaId = text(head?.source_rca_id);
    const engine = text(head?.engine);
    const item: LibraryItem = {
      playbook_id: id,
      revision,
      source_rca_id: rcaId,
      engine,
      failure_type: text(book?.failure_type ?? head?.failure_type),
      symptom_pattern: text(book?.symptom_pattern ?? head?.symptom_pattern),
      tags: stringList(book?.tags ?? head?.tags),
      verification_status:
        text(book?.verification_status ?? head?.verification_status) || 'DRAFT',
      updated_at: text(head?.updated_at) || null,
      publication_status:
        head?.publication_status === 'PENDING' ? 'PENDING' : 'PUBLISHED',
      availability: reason ? 'UNAVAILABLE' : 'AVAILABLE',
      unavailable_reason: reason,
    };
    return {
      item,
      playbook:
        book && !reason
          ? {
              ...publishedPayload(book),
              library_revision: revision,
              source_engine: engine,
              source_rca_id: rcaId,
            }
          : null,
    };
  }
  /** Retain display and publication metadata for 90 days without copying any full original body. */
  function libraryState(head: Row, book: Row): Row {
    const revisionCreatedAt = Date.parse(text(head.updated_at));
    if (!Number.isFinite(revisionCreatedAt))
      libraryError(
        409,
        'INVALID_REVISION_TIME',
        '상태 보존 기한의 기준 시각을 확인할 수 없습니다.',
      );
    return {
      PK: LIBRARY_STATE,
      SK: head.SK,
      revision: head.revision,
      source_rca_id: head.source_rca_id,
      proposal_rca_id: head.proposal_rca_id,
      engine: head.engine,
      publication_status: head.publication_status,
      vector_key: head.vector_key,
      metric_name: head.metric_name,
      updated_at: head.updated_at,
      original_expires_at: head.ttl,
      original_created_at: head.original_created_at,
      failure_type: text(book.failure_type),
      symptom_pattern: [...text(book.symptom_pattern)].slice(0, 240).join(''),
      tags: stringList(book.tags),
      verification_status: text(book.verification_status) || 'DRAFT',
      ttl: Math.floor(revisionCreatedAt / 1000) + STATE_RETENTION,
    };
  }
  /**
   * Read current public knowledge with the canonical head taking precedence.
   * Never resurrect legacy content behind a broken head; verify retrospective commit identity.
   */
  async function detail(
    id: string,
    hint?: { source_rca_id?: string; engine?: string },
  ): Promise<LibraryDetail> {
    identity(id);
    const head = await get('PLAYBOOK_LIBRARY', id);
    const state = await get(LIBRARY_STATE, id);
    if (state && !retained(state, true))
      return describe(
        id,
        null,
        null,
        '플레이북 상태의 보존 기간이 지났습니다.',
      );
    if (state && !head)
      return describe(
        id,
        state,
        null,
        '플레이북 원문이 만료되었거나 보존 원본을 조회할 수 없습니다.',
      );
    if (head) {
      const book = libraryBody(head.playbook_json, id);
      if (
        bodyDeadline(head) <= now() ||
        !book ||
        book.playbook_id !== id ||
        !text(head.revision) ||
        head.vector_key !== `${id}@${head.revision}`
      )
        return describe(
          id,
          state ?? head,
          null,
          '현재 개정본이 만료되었거나 형식이 올바르지 않습니다.',
        );
      if (
        state &&
        (state.revision !== head.revision ||
          state.publication_status !== head.publication_status)
      )
        return describe(
          id,
          state,
          null,
          '현재 개정본과 게시 상태가 일치하지 않습니다.',
        );
      try {
        const src = await source(text(head.source_rca_id), text(head.engine));
        if (src.book.playbook_id !== id)
          return describe(
            id,
            head,
            book,
            '완료 원본의 현재 플레이북 식별자가 달라졌습니다.',
          );
        if (
          head.revision.startsWith('retrospective:') &&
          (src.sourceItem.SK !== playbookRevisionSk(head.engine) ||
            src.sourceItem.publication_status !== 'PUBLISHED' ||
            src.sourceItem.revised_by_execution_id !==
              head.revision.slice('retrospective:'.length) ||
            !isDeepStrictEqual(
              publishedPayload(src.book),
              publishedPayload(book),
            ))
        )
          return describe(
            id,
            head,
            book,
            '정식 회고 개정본의 게시 식별자 또는 원문이 일치하지 않습니다.',
          );
      } catch {
        return describe(
          id,
          head,
          book,
          '연결된 완료 원본을 조회할 수 없습니다.',
        );
      }
      if (head.publication_status !== 'PUBLISHED')
        return describe(
          id,
          head,
          book,
          '반영된 지식의 검색 게시가 대기 중입니다.',
        );
      return describe(id, head, book, null);
    }
    const response = await vectors.send(
      new GetVectorsCommand({
        ...vectorIndex(),
        keys: [id],
        returnMetadata: true,
      }),
    );
    const vector = response.vectors?.find((entry: Row) => entry.key === id);
    const metadata = object(vector?.metadata);
    if (!metadata || metadata.library_revision)
      return describe(
        id,
        null,
        null,
        '현재 게시된 플레이북을 찾을 수 없습니다.',
      );
    const rcaId = text(metadata.rca_id);
    const engine =
      text(metadata.engine) || (await legacyEngine(rcaId, id).catch(() => ''));
    const legacyHead = {
      source_rca_id: rcaId,
      engine,
      updated_at: metadata.updated_at,
    };
    if (
      (hint?.source_rca_id && hint.source_rca_id !== rcaId) ||
      (hint?.engine && hint.engine !== engine)
    )
      return describe(
        id,
        legacyHead,
        null,
        '요청한 원본과 현재 검색 참조가 다릅니다.',
      );
    try {
      const src = await source(rcaId, engine);
      if (
        legacyOriginalDeadline(src.sourceItem, src.session) <= now() ||
        src.book.playbook_id !== id ||
        (src.session.playbook_index_status &&
          src.session.playbook_index_status !== 'PUBLISHED') ||
        (metadata.publication_id &&
          src.sourceItem.revised_by_execution_id !== metadata.publication_id) ||
        (src.sourceItem.publication_status &&
          src.sourceItem.publication_status !== 'PUBLISHED')
      )
        return describe(
          id,
          legacyHead,
          null,
          '현재 원문과 게시 참조가 일치하지 않습니다.',
        );
      // A head created while hydrating legacy owns the latest version.
      if ((await get('PLAYBOOK_LIBRARY', id)) || (await get(LIBRARY_STATE, id)))
        return detail(id, hint);
      return describe(
        id,
        { ...legacyHead, updated_at: src.session.updated_at },
        src.book,
        null,
      );
    } catch {
      return describe(
        id,
        legacyHead,
        null,
        '완료 원문이 만료되었거나 조회할 수 없습니다.',
      );
    }
  }
  /**
   * Browse summary state, pre-split heads and legacy vectors without scanning incident records.
   * Bind cursors to filters and preserve continuation even when a page has no matches.
   */
  async function list(query: Record<string, unknown>): Promise<LibraryPage> {
    const limit = Math.min(
      100,
      Math.max(1, Math.floor(Number(query.limit)) || 25),
    );
    const filters = {
      failure_type: text(query.failure_type),
      tag: text(query.tag),
      verification_status: text(query.verification_status),
    };
    let cursor: Row = { phase: 'state', filters };
    if (query.cursor) {
      try {
        cursor = JSON.parse(
          Buffer.from(String(query.cursor), 'base64url').toString(),
        );
        // Preserve in-flight cursors from the earlier STATE-only listing.
        if (cursor.phase === 'heads' && cursor.key?.PK === LIBRARY_STATE)
          cursor.phase = 'state';
        if (
          !['state', 'heads', 'legacy'].includes(cursor.phase) ||
          !isDeepStrictEqual(cursor.filters, filters)
        )
          throw new Error();
        if (
          cursor.key &&
          (!['state', 'heads'].includes(cursor.phase) ||
            cursor.key.PK !==
              (cursor.phase === 'state' ? LIBRARY_STATE : 'PLAYBOOK_LIBRARY') ||
            typeof cursor.key.SK !== 'string')
        )
          throw new Error();
        if (cursor.token && typeof cursor.token !== 'string') throw new Error();
      } catch {
        libraryError(
          400,
          'INVALID_CURSOR',
          '목록 커서 또는 필터가 올바르지 않습니다.',
        );
      }
    }
    const items: LibraryItem[] = [];
    let next: Row | null;
    if (cursor.phase === 'state' || cursor.phase === 'heads') {
      const partition =
        cursor.phase === 'state' ? LIBRARY_STATE : 'PLAYBOOK_LIBRARY';
      const page = await ddb.send(
        new QueryCommand({
          TableName: table,
          ConsistentRead: true,
          KeyConditionExpression: 'PK = :pk',
          ExpressionAttributeValues: { ':pk': partition },
          Limit: limit,
          ExclusiveStartKey: cursor.key,
        }),
      );
      for (const head of page.Items ?? []) {
        if (!retained(head, true)) continue;
        if (cursor.phase === 'heads' && (await get(LIBRARY_STATE, head.SK)))
          continue;
        items.push((await detail(head.SK)).item);
      }
      next = page.LastEvaluatedKey
        ? { phase: cursor.phase, key: page.LastEvaluatedKey, filters }
        : { phase: cursor.phase === 'state' ? 'heads' : 'legacy', filters };
      if (!items.length && next.phase !== cursor.phase)
        return list({
          ...query,
          cursor: Buffer.from(JSON.stringify(next)).toString('base64url'),
        });
    } else {
      const page = await vectors.send(
        new ListVectorsCommand({
          ...vectorIndex(),
          maxResults: limit,
          nextToken: cursor.token,
          returnMetadata: true,
        }),
      );
      const seen = new Set<string>();
      for (const vector of page.vectors ?? []) {
        const metadata = object(vector.metadata);
        if (metadata?.library_revision) continue;
        const id = text(metadata?.playbook_id) || text(vector.key);
        // Legacy keys are the playbook id; managed revision keys never hydrate legacy.
        if (!id || vector.key !== id || seen.has(id)) continue;
        seen.add(id);
        if (
          (await get('PLAYBOOK_LIBRARY', id)) ||
          (await get(LIBRARY_STATE, id))
        )
          continue;
        items.push((await detail(id)).item);
      }
      next = page.nextToken
        ? { phase: 'legacy', token: page.nextToken, filters }
        : null;
    }
    return {
      items: items.filter(
        (item) =>
          (!filters.failure_type ||
            item.failure_type
              .toLocaleLowerCase()
              .includes(filters.failure_type.toLocaleLowerCase())) &&
          (!filters.tag || item.tags.includes(filters.tag)) &&
          (!filters.verification_status ||
            item.verification_status === filters.verification_status),
      ),
      nextCursor: next
        ? Buffer.from(JSON.stringify(next)).toString('base64url')
        : null,
    };
  }
  /** Resolve only the original named by the thin reference; a missing archive cannot fall back to state. */
  async function comparisonOriginal(
    src: Awaited<ReturnType<typeof source>>,
    rcaId: string,
    engine: string,
  ) {
    const reference =
      object(src.originalBook?.comparison) ?? object(src.book.comparison);
    if (!reference)
      return {
        reference: null,
        comparison: null,
        expiresAt: 0,
        reason: '이 분석에는 생성 참고 자료가 제공되지 않았습니다.',
      };
    if ('original_sk' in reference) {
      const sk = text(reference.original_sk);
      const deadline = Number(reference.original_expires_at);
      if (
        !sk.startsWith(`${engine}#PLAYBOOK_COMPARISON#`) ||
        !Number.isFinite(deadline) ||
        deadline <= now()
      )
        return {
          reference,
          comparison: null,
          expiresAt: deadline || 0,
          reason:
            '생성 참고 자료 원문의 60일 보존 기간이 지났거나 원본 참조가 올바르지 않습니다.',
        };
      let row: Row | null;
      try {
        row = await get(rcaPk(rcaId), sk);
      } catch {
        return {
          reference,
          comparison: null,
          expiresAt: deadline,
          reason: '고정된 생성 참고 자료 원문을 불러오지 못했습니다.',
        };
      }
      const expiresAt = Math.min(deadline, row ? bodyDeadline(row) : 0);
      const comparison =
        typeof row?.comparison_json === 'string'
          ? object(row.comparison_json)
          : null;
      if (
        !row ||
        expiresAt <= now() ||
        !comparison ||
        (row.engine && row.engine !== engine) ||
        (row.source_rca_id && row.source_rca_id !== rcaId) ||
        comparison.status !== reference.status ||
        text(comparison.selected_playbook_id) !==
          text(reference.selected_playbook_id) ||
        text(comparison.proposal?.proposal_id) !==
          text(reference.proposal?.proposal_id)
      )
        return {
          reference,
          comparison: null,
          expiresAt,
          reason:
            '고정된 생성 참고 자료 원문이 만료되었거나 조회할 수 없습니다.',
        };
      src.guards.push(row);
      const recordedCreation = originalEpoch(
        row.original_created_at ?? row.created_at ?? row.updated_at,
      );
      const originalCreatedAt = Number.isFinite(recordedCreation)
        ? new Date(recordedCreation * 1000).toISOString()
        : new Date((deadline - ORIGINAL_RETENTION) * 1000).toISOString();
      return {
        reference,
        comparison,
        expiresAt,
        originalCreatedAt,
        reason: null,
      };
    }
    if (!Array.isArray(reference.candidates)) {
      return {
        reference,
        comparison: null,
        expiresAt: 0,
        reason:
          text(reference.failure_reason) ||
          '생성 참고 자료 원문을 가리키는 참조가 기록되지 않았습니다.',
      };
    }
    // Early inline records predate archiving; the session's 90-day TTL cannot extend their originals.
    const expiresAt = legacyOriginalDeadline(src.sourceItem, src.session);
    return expiresAt > now()
      ? {
          reference,
          comparison: reference,
          expiresAt,
          originalCreatedAt: new Date(
            (expiresAt - ORIGINAL_RETENTION) * 1000,
          ).toISOString(),
          reason: null,
        }
      : {
          reference,
          comparison: null,
          expiresAt,
          reason:
            '과거 생성 참고 자료 원문이 만료되었거나 생성 시각을 확인할 수 없습니다.',
        };
  }

  /** Keep only outcome metadata after original expiry; never place baseline, evidence or diffs in 90-day state. */
  function comparisonSummary(
    reference: Row | null,
  ): ProposalResponse['summary'] {
    if (!reference) return null;
    const summary: NonNullable<ProposalResponse['summary']> = {
      status: reference.status,
      selected_playbook_id:
        typeof reference.selected_playbook_id === 'string'
          ? reference.selected_playbook_id
          : null,
    };
    if (reference.proposal?.proposal_id)
      summary.proposal = {
        proposal_id: reference.proposal.proposal_id,
        state: reference.proposal.state,
      };
    return summary;
  }

  /** Read the exact 60-day original while independently overlaying the retained human decision. */
  async function readProposal(
    rcaId: string,
    engine: string,
  ): Promise<ProposalResponse> {
    const src = await source(rcaId, engine, true);
    const frozen = await comparisonOriginal(src, rcaId, engine);
    const summary = comparisonSummary(frozen.reference);
    const comparison = frozen.comparison
      ? (structuredClone(frozen.comparison) as PlaybookComparison)
      : null;
    if (summary?.proposal) {
      const disposition = await get(
        rcaPk(rcaId),
        `${engine}#PLAYBOOK_PROPOSAL#${summary.proposal.proposal_id}`,
      );
      if (disposition && retained(disposition, true)) {
        const overlay = {
          state: disposition.state,
          result_revision: disposition.result_revision,
          publication_status: disposition.publication_status,
          publication_error: disposition.publication_error,
        };
        Object.assign(summary.proposal, overlay);
        if (comparison?.proposal) Object.assign(comparison.proposal, overlay);
      }
    }
    return {
      rca_id: rcaId,
      engine,
      comparison,
      summary,
      unavailable_reason: frozen.reason,
    };
  }
  /**
   * Limit human knowledge updates to the allowed fields while preserving the runbook.
   * Reject malformed bodies, deleted fields and diffs that disagree with before/after.
   */
  function validateProposal(proposal: PlaybookProposal) {
    const before = object(proposal.before);
    const after = object(proposal.after);
    if (
      !before ||
      !after ||
      before.playbook_id !== proposal.playbook_id ||
      after.playbook_id !== proposal.playbook_id
    )
      libraryError(
        409,
        'INVALID_PROPOSAL',
        '제안 원본의 식별자가 일치하지 않습니다.',
      );
    const cleanBefore = domainPlaybook(before);
    const cleanAfter = domainPlaybook(after);
    if (
      !libraryBody(cleanBefore, proposal.playbook_id) ||
      !libraryBody(cleanAfter, proposal.playbook_id)
    )
      libraryError(
        409,
        'INVALID_PROPOSAL',
        '제안 플레이북의 필드 형식이 올바르지 않습니다.',
      );
    const changed = [
      ...new Set([...Object.keys(cleanBefore), ...Object.keys(cleanAfter)]),
    ].filter((key) => !isDeepStrictEqual(cleanBefore[key], cleanAfter[key]));
    for (const field of [
      'tags',
      'verification_steps',
      'prevention_measures',
      'related_metrics',
    ]) {
      const previous = cleanBefore[field];
      const proposed = cleanAfter[field];
      if (
        Array.isArray(previous) &&
        (!Array.isArray(proposed) ||
          previous.some((entry) => !proposed.includes(entry)))
      )
        libraryError(
          409,
          'KNOWLEDGE_OMISSION',
          '제안 목록에서 기존 대응 지식이 빠졌습니다. 기존 항목을 보존한 변경 전후로 다시 비교해야 합니다.',
        );
    }
    if (
      !changed.length ||
      changed.some(
        (key) =>
          !KNOWLEDGE_FIELDS.has(key) ||
          !(key in cleanAfter) ||
          cleanAfter[key] === null ||
          cleanAfter[key] === '' ||
          (Array.isArray(cleanAfter[key]) && !cleanAfter[key].length),
      )
    )
      libraryError(
        409,
        'INVALID_PROPOSAL',
        '대응 지식만 보강할 수 있으며 기존 필드를 삭제할 수 없습니다.',
      );
    if (
      !Array.isArray(proposal.changes) ||
      proposal.changes.length !== changed.length ||
      new Set(proposal.changes.map((entry) => entry.field)).size !==
        changed.length ||
      proposal.changes.some(
        (entry) =>
          !changed.includes(entry.field) ||
          !isDeepStrictEqual(
            entry.before ?? null,
            cleanBefore[entry.field] ?? null,
          ) ||
          !isDeepStrictEqual(entry.after, cleanAfter[entry.field]),
      )
    )
      libraryError(
        409,
        'INVALID_PROPOSAL',
        '항목별 변경 내역이 원문과 일치하지 않습니다.',
      );
    return { before: cleanBefore, after: cleanAfter };
  }
  /**
   * Retry search publication from the applied immutable snapshot, not a fresh proposal.
   * Write its revision-specific vector before conditionally publishing the matching head.
   */
  async function publish(disposition: Row): Promise<void> {
    const id = disposition.playbook_id;
    const revision = disposition.result_revision;
    const head = await get('PLAYBOOK_LIBRARY', id);
    if (!head || head.revision !== revision)
      libraryError(
        409,
        'PUBLICATION_SUPERSEDED',
        '다른 개정본이 현재 지식이 되어 이전 게시를 다시 수행할 수 없습니다.',
      );
    if (bodyDeadline(head) <= now())
      libraryError(
        409,
        'PUBLICATION_EXPIRED',
        '게시할 개정본의 보존 기간이 지났습니다.',
      );
    if (head.publication_status === 'PUBLISHED') return;
    const state = await get(LIBRARY_STATE, id);
    if (
      !state ||
      !retained(state, true) ||
      state.revision !== revision ||
      state.publication_status !== 'PENDING' ||
      Number(state.original_expires_at) <= now()
    )
      libraryError(
        409,
        'PUBLICATION_STATE_UNAVAILABLE',
        '현재 게시 상태 또는 원문 보존 기한을 확인할 수 없습니다.',
      );
    const snapshot = await get(`PLAYBOOK#${id}`, revision);
    if (
      !snapshot ||
      bodyDeadline(snapshot) <= now() ||
      snapshot.playbook_json !== head.playbook_json ||
      snapshot.revision !== revision ||
      snapshot.source_rca_id !== head.source_rca_id ||
      snapshot.engine !== head.engine ||
      snapshot.vector_key !== `${id}@${revision}`
    )
      libraryError(
        409,
        'SNAPSHOT_UNAVAILABLE',
        '게시할 고정 개정본을 확인할 수 없습니다.',
      );
    const origin = await source(text(head.source_rca_id), text(head.engine));
    if (origin.book.playbook_id !== id)
      libraryError(
        409,
        'SOURCE_UNAVAILABLE',
        '게시 대상의 완료 원본이 변경되었습니다.',
      );
    const book = object(snapshot.playbook_json)!;
    const encoded = await embedding.send(
      new InvokeModelCommand({
        modelId: config.bedrockEmbeddingModelId,
        contentType: 'application/json',
        accept: 'application/json',
        body: Buffer.from(
          JSON.stringify({
            texts: [libraryEmbedKey(book, text(snapshot.metric_name))],
            input_type: 'search_document',
            embedding_types: ['float'],
          }),
        ),
      }),
    );
    const data = JSON.parse(new TextDecoder().decode(encoded.body)).embeddings
      ?.float?.[0];
    if (
      !Array.isArray(data) ||
      !data.length ||
      data.some((n: unknown) => typeof n !== 'number' || !Number.isFinite(n))
    )
      throw new Error('임베딩 응답을 읽을 수 없습니다.');
    await vectors.send(
      new PutVectorsCommand({
        ...vectorIndex(),
        vectors: [
          {
            key: snapshot.vector_key,
            data: { float32: data },
            metadata: {
              playbook_id: id,
              library_revision: revision,
              rca_id: snapshot.source_rca_id,
              engine: snapshot.engine,
              verification_status: text(book.verification_status) || 'DRAFT',
            },
          },
        ],
      }),
    );
    await ddb.send(
      new TransactWriteCommand({
        TransactItems: [
          ...checks([origin]),
          {
            Update: {
              TableName: table,
              Key: { PK: 'PLAYBOOK_LIBRARY', SK: id },
              UpdateExpression: 'SET publication_status = :published',
              ConditionExpression:
                '#revision = :revision AND playbook_json = :body',
              ExpressionAttributeNames: { '#revision': 'revision' },
              ExpressionAttributeValues: {
                ':published': 'PUBLISHED',
                ':revision': revision,
                ':body': head.playbook_json,
              },
            },
          },
          {
            ConditionCheck: {
              TableName: table,
              Key: { PK: `PLAYBOOK#${id}`, SK: revision },
              ...condition(snapshot),
            },
          },
          {
            Update: {
              TableName: table,
              Key: { PK: LIBRARY_STATE, SK: id },
              UpdateExpression: 'SET publication_status = :published',
              ...condition(state),
              ExpressionAttributeValues: {
                ...condition(state).ExpressionAttributeValues,
                ':published': 'PUBLISHED',
              },
            },
          },
          {
            Update: {
              TableName: table,
              Key: { PK: disposition.PK, SK: disposition.SK },
              UpdateExpression:
                'SET publication_status = :published REMOVE publication_error',
              ConditionExpression:
                '#state = :applied AND result_revision = :revision',
              ExpressionAttributeNames: { '#state': 'state' },
              ExpressionAttributeValues: {
                ':published': 'PUBLISHED',
                ':applied': 'APPLIED',
                ':revision': revision,
              },
            },
          },
        ],
      }),
    );
    if (
      disposition.previous_vector_key &&
      disposition.previous_vector_key !== snapshot.vector_key
    ) {
      try {
        await vectors.send(
          new DeleteVectorsCommand({
            ...vectorIndex(),
            keys: [disposition.previous_vector_key],
          }),
        );
      } catch {
        /* Old immutable vectors are rejected by revision validation. */
      }
    }
  }
  /**
   * Apply or reject the proposal owned by the requested completed incident and engine.
   * Commit baseline/source checks with the durable decision; retries cannot reverse it.
   */
  async function act(
    rcaId: string,
    engine: string,
    proposalId: string,
    action: string,
  ): Promise<ProposalResponse> {
    identity(rcaId, engine);
    identity(proposalId);
    if (!/^[A-Za-z0-9._:-]{1,160}$/.test(proposalId))
      libraryError(
        400,
        'INVALID_IDENTITY',
        '제안 식별자의 형식이 올바르지 않습니다.',
      );
    if (!['apply', 'reject'].includes(action))
      libraryError(400, 'INVALID_ACTION', '반영 또는 기각을 선택하세요.');
    const key = {
      PK: rcaPk(rcaId),
      SK: `${engine}#PLAYBOOK_PROPOSAL#${proposalId}`,
    };
    let disposition = await get(key.PK, key.SK);
    if (disposition) {
      if (!retained(disposition))
        libraryError(
          409,
          'DISPOSITION_EXPIRED',
          '처리 결과의 보존 기간이 지났습니다.',
        );
      if (disposition.state !== (action === 'apply' ? 'APPLIED' : 'REJECTED'))
        libraryError(
          409,
          'ALREADY_DECIDED',
          '이미 반대 동작으로 처리된 제안입니다.',
        );
    } else {
      const current = await source(rcaId, engine, true);
      const frozen = await comparisonOriginal(current, rcaId, engine);
      if (!frozen.comparison)
        libraryError(
          409,
          'COMPARISON_ORIGINAL_UNAVAILABLE',
          frozen.reason || '생성 참고 자료 원문을 확인할 수 없습니다.',
        );
      const comparison = frozen.comparison;
      const proposal = object(comparison?.proposal) as PlaybookProposal | null;
      if (
        !proposal ||
        proposal.proposal_id !== proposalId ||
        comparison?.status !== 'UPDATE_PROPOSED'
      )
        libraryError(
          404,
          'PROPOSAL_NOT_FOUND',
          '이 사고의 변경 제안을 찾을 수 없습니다.',
        );
      if (proposal.state !== 'PENDING')
        libraryError(
          409,
          'INVALID_PROPOSAL',
          '원본 제안이 검토 대기 상태가 아닙니다.',
        );
      const stamp = new Date(now() * 1000).toISOString();
      disposition = {
        ...key,
        state: action === 'apply' ? 'APPLIED' : 'REJECTED',
        playbook_id: proposal.playbook_id,
        updated_at: stamp,
        requested_by: 'dashboard',
        ttl: now() + STATE_RETENTION,
      };
      let transaction: Transaction = checks([current]);
      if (action === 'apply') {
        const { before, after } = validateProposal(proposal);
        const available = await detail(proposal.playbook_id, {
          source_rca_id: proposal.source_rca_id,
          engine: proposal.source_engine,
        });
        if (
          available.item.availability !== 'AVAILABLE' ||
          available.item.revision !== proposal.base_revision ||
          !isDeepStrictEqual(domainPlaybook(available.playbook!), before)
        )
          libraryError(
            409,
            'STALE_BASELINE',
            '기준 개정본이 변경되었거나 원문을 읽을 수 없습니다. 최신 지식으로 다시 비교해야 합니다.',
          );
        const historical = await source(
          available.item.source_rca_id,
          available.item.engine,
        );
        const head = await get('PLAYBOOK_LIBRARY', proposal.playbook_id);
        const previousState = await get(LIBRARY_STATE, proposal.playbook_id);
        if (
          (head &&
            (head.revision !== proposal.base_revision ||
              head.publication_status !== 'PUBLISHED')) ||
          (!head && proposal.base_revision !== 'legacy')
        )
          libraryError(409, 'STALE_BASELINE', '기준 개정본이 변경되었습니다.');
        if (
          previousState &&
          (!head ||
            previousState.revision !== head.revision ||
            previousState.publication_status !== 'PUBLISHED' ||
            !retained(previousState, true))
        )
          libraryError(
            409,
            'STALE_BASELINE',
            '기준 개정본의 상태가 변경되었습니다.',
          );
        if (
          head &&
          !isDeepStrictEqual(
            domainPlaybook(object(head.playbook_json) ?? {}),
            before,
          )
        )
          libraryError(409, 'STALE_BASELINE', '기준 원문이 변경되었습니다.');
        if (
          !head &&
          !isDeepStrictEqual(domainPlaybook(historical.book), before)
        )
          libraryError(409, 'STALE_BASELINE', '완료 원본이 변경되었습니다.');
        const revision = `proposal:${proposalId}`;
        if (`${proposal.playbook_id}@${revision}`.length > 1024)
          libraryError(400, 'INVALID_IDENTITY', '게시 식별자가 너무 깁니다.');
        const nextHead: Row = {
          PK: 'PLAYBOOK_LIBRARY',
          SK: proposal.playbook_id,
          playbook_json: JSON.stringify(after),
          revision,
          source_rca_id: available.item.source_rca_id,
          proposal_rca_id: rcaId,
          engine: available.item.engine,
          publication_status: 'PENDING',
          vector_key: `${proposal.playbook_id}@${revision}`,
          metric_name:
            text(head?.metric_name) || text(historical.session.metric_name),
          updated_at: stamp,
          original_created_at: frozen.originalCreatedAt,
          ttl: Math.min(now() + ORIGINAL_RETENTION, frozen.expiresAt),
        };
        nextHead.original_expires_at = nextHead.ttl;
        Object.assign(disposition, {
          result_revision: revision,
          publication_status: 'PENDING',
          previous_vector_key: text(head?.vector_key) || proposal.playbook_id,
        });
        transaction = checks([current, historical]);
        transaction.push(
          {
            Put: {
              TableName: table,
              Item: libraryState(nextHead, after),
              ...(previousState
                ? condition(previousState)
                : { ConditionExpression: 'attribute_not_exists(PK)' }),
            },
          },
          {
            Put: {
              TableName: table,
              Item: nextHead,
              ...(head
                ? condition(head)
                : { ConditionExpression: 'attribute_not_exists(PK)' }),
            },
          },
          {
            Put: {
              TableName: table,
              Item: {
                ...nextHead,
                PK: `PLAYBOOK#${proposal.playbook_id}`,
                SK: revision,
              },
              ConditionExpression: 'attribute_not_exists(PK)',
            },
          },
        );
      }
      transaction.push({
        Put: {
          TableName: table,
          Item: disposition,
          ConditionExpression: 'attribute_not_exists(PK)',
        },
      });
      try {
        await ddb.send(
          new TransactWriteCommand({ TransactItems: transaction }),
        );
      } catch (err: any) {
        if (
          err.name !== 'TransactionCanceledException' &&
          err.name !== 'ConditionalCheckFailedException'
        )
          throw err;
        const decided = await get(key.PK, key.SK);
        if (!decided)
          libraryError(
            409,
            'STALE_BASELINE',
            '원본 또는 기준 개정본이 변경되었습니다. 다시 비교해야 합니다.',
          );
        if (decided.state !== disposition.state)
          libraryError(
            409,
            'ALREADY_DECIDED',
            '이미 반대 동작으로 처리된 제안입니다.',
          );
        disposition = decided;
      }
    }
    let publicationError = '';
    if (action === 'apply' && disposition.publication_status !== 'PUBLISHED') {
      const owner = await source(rcaId, engine, true);
      const original = await comparisonOriginal(owner, rcaId, engine);
      if (!original.comparison)
        libraryError(
          409,
          'COMPARISON_ORIGINAL_UNAVAILABLE',
          original.reason || '게시할 제안 원문이 만료되었습니다.',
        );
      try {
        await publish(disposition);
      } catch (err: any) {
        publicationError =
          err?.data?.code === 'PUBLICATION_SUPERSEDED' ||
          err?.data?.code === 'PUBLICATION_EXPIRED'
            ? err.statusMessage
            : '지식은 반영되었으나 검색 게시가 완료되지 않았습니다. 같은 반영을 다시 요청하면 게시만 재시도합니다.';
      }
    }
    const response = await readProposal(rcaId, engine);
    if (
      publicationError &&
      response.comparison?.proposal?.publication_status === 'PENDING'
    )
      response.comparison.proposal.publication_error = publicationError;
    return response;
  }
  return { detail, list, readProposal, act };
}

/**
 * Bind the handlers to configured local AWS clients without adding a write on read.
 * The same service contract is exercised by the handler tests' fake clients.
 */
export function usePlaybookLibrary() {
  return createPlaybookLibrary({
    ddb: useDynamoDB(),
    vectors: useS3Vectors(),
    embedding: useEmbeddingRuntime(),
    config: useRuntimeConfig(),
  });
}
