// Dataset projection only: no evaluator policy, model execution, or DB calls.
// The original proof and these selectors belong to operators, never to prompts.
import assert from 'node:assert/strict';

const CASES = {
  'exception-session-cleanup': 'exception',
  'maintenance-transaction-lock': 'lock',
  'pool-config-regression': 'pool',
  'query-amplification': 'query',
};
/** Copy retained evidence without letting a projection mutate the archived proof. */
const clone = (value) => structuredClone(value);
/** Preserve only explicitly allowed, present fields; never synthesize missing values. */
const pick = (value, keys) =>
  Object.fromEntries(
    keys
      .filter((key) => Object.hasOwn(value, key))
      .map((key) => [key, clone(value[key])]),
  );

/** Require recorded UTC timestamps so the projection cannot invent a capture window. */
function iso(value, label) {
  assert.equal(
    typeof value,
    'string',
    `${label} must be a recorded UTC timestamp`,
  );
  assert.match(value, /(?:Z|\+00:00)$/);
  assert.ok(Number.isFinite(Date.parse(value)), `${label} must parse`);
  return value;
}

/**
 * Bound baseline and incident records by their measured timestamps.
 * Lock observations stop before recovery, and out-of-window requests are rejected.
 */
function phaseData(proof, caseName, phase) {
  const index = proof.phases.findIndex(
    (item) => item.case === caseName && item.phase === phase,
  );
  assert.ok(index >= 0, `missing ${caseName}/${phase}`);
  const raw = proof.phases[index];
  const config = raw.events.find((event) => event.event === 'db_pool_config');
  assert.ok(config);
  const start = iso(raw.measurement_started_at, 'measurement_started_at');
  const end = iso(
    caseName === 'lock' && phase === 'fault'
      ? raw.blocked_write.completed_at
      : raw.measurement_completed_at,
    'pre-recovery observation cutoff',
  );
  assert.ok(Date.parse(start) <= Date.parse(end));
  const configTime = iso(config.timestamp, 'configuration timestamp');
  const window = {
    start: Date.parse(configTime) < Date.parse(start) ? configTime : start,
    end,
  };
  const writes =
    caseName === 'pool'
      ? raw.writes
      : caseName === 'query'
        ? raw.queries
        : caseName === 'exception'
          ? [raw.baseline_write, ...raw.probes, raw.subsequent_write]
          : phase === 'normal'
            ? [raw.write]
            : [raw.baseline_write, raw.blocked_write];
  for (const write of writes) {
    iso(write.started_at, 'request start');
    iso(write.completed_at, 'request end');
    assert.ok(Date.parse(write.started_at) >= Date.parse(window.start));
    assert.ok(Date.parse(write.completed_at) <= Date.parse(end));
  }
  return {
    index,
    raw,
    config,
    window,
    writes,
    context: phase === 'normal' ? 'baseline' : 'incident',
  };
}

/** Retain measured SQL and connection counters without operation labels or bound values. */
const operation = (write) =>
  pick(write.operation, [
    'request_id',
    'sql_count',
    'sql_time_ms',
    'checkouts',
    'checkins',
    'acquire_time_ms',
    'patterns',
  ]);
/** Tie connection return counts to the recorded request window and identifier. */
const lifetime = (write) => ({
  ...pick(write, ['started_at', 'completed_at', 'checked_out_after_request']),
  ...pick(write.operation, [
    'request_id',
    'checkouts',
    'checkins',
    'acquire_time_ms',
  ]),
});
/** Keep observed request outcomes and response hashes without copying response data. */
const result = (write) => ({
  ...pick(write, [
    'started_at',
    'completed_at',
    'outcome',
    'error_type',
    'elapsed_ms',
    'saved_rows',
    'row_count',
    'rows_sha256',
    'checked_out_after_request',
  ]),
  request_id: write.operation.request_id,
});
/** Keep measured revision identities and causal source hashes without private phase labels. */
function source(raw) {
  return {
    ...pick(raw.source, ['revision', 'fingerprint']),
    files: pick(raw.source.files, ['revision/query.py', 'revision/session.py']),
  };
}
/** Expose the observed lock relationship using metadata rather than SQL or row values. */
const lockRows = (rows) =>
  rows.map((row) =>
    pick(row, ['pid', 'locktype', 'mode', 'granted', 'relation']),
  );
/** Project measured DB state through a field allowlist that excludes SQL values and recovery. */
function snapshot(value) {
  assert.ok(value, 'a measured DB snapshot is required');
  return {
    ...pick(value, [
      'event',
      'sql_hash_algorithm',
      'pool_checked_out',
      'pool_size',
      'server_max_connections',
      'database_connections',
    ]),
    activity: value.activity.map((row) =>
      pick(row, [
        'pid',
        'application_name',
        'state',
        'wait_event_type',
        'wait_event',
        'xact_start',
        'query_start',
        'blocking_pids',
        'sql_hash',
      ]),
    ),
    locks: lockRows(value.locks),
  };
}
/** Retain owned lock facts and deadlines while excluding subsequent stop and rollback results. */
function maintenance(value) {
  return {
    ...pick(value, [
      'event',
      'run_id',
      'backend_pid',
      'hold_seconds',
      'max_hold_seconds',
      'acquired_at',
      'expires_at',
      'transaction_start',
    ]),
    locks: lockRows(value.locks),
  };
}
const units = {
  started_at: 'UTC timestamp',
  completed_at: 'UTC timestamp',
  timestamp: 'UTC timestamp',
  elapsed_ms: 'Milliseconds',
  acquire_time_ms: 'Milliseconds',
  sql_time_ms: 'Milliseconds',
  time_ms: 'Milliseconds',
  sql_count: 'Count',
  count: 'Count',
  checkouts: 'Count',
  checkins: 'Count',
  checked_out_after_request: 'Count',
  row_count: 'Count',
  saved_rows: 'Count',
  concurrency: 'Count',
  batch_rows: 'Count',
  pool_size: 'Count',
  max_overflow: 'Count',
  pool_timeout_seconds: 'Seconds',
  statement_timeout_ms: 'Milliseconds',
  server_max_connections: 'Count',
  database_connections: 'Count',
  pool_checked_out: 'Count',
};

/**
 * Return only baseline + incident observations, and a separate operator mapping.
 * Changing any restoration/cleanup result cannot change the returned model input.
 * snippets is a reviewed source excerpt whose content hash matches the measured build.
 */
export function projectIncidentCaptures(scenarioId, proof, snippets = {}) {
  const caseName = CASES[scenarioId];
  assert.ok(caseName, 'unknown catalog id');
  const baseline = phaseData(proof, caseName, 'normal');
  const incident = phaseData(proof, caseName, 'fault');
  const pair = [baseline, incident];
  const resource = { runId: proof.run_id, schema: proof.schema };
  const operatorMapping = {};
  const ordinal = Object.keys(CASES).indexOf(scenarioId) + 1;
  /** Give each capture a neutral identity and keep raw phase pointers operator-only. */
  const wrap = (number, select, pointerSuffixes = []) => {
    const captureId = `capture-${ordinal}${String(number).padStart(3, '0')}`;
    const records = pair.map((item) => ({
      context: item.context,
      window: item.window,
      ...select(item),
    }));
    operatorMapping[captureId] = pair.flatMap(({ index, raw }) =>
      pointerSuffixes
        .filter(
          (suffix) =>
            suffix
              .slice(1)
              .split('/')
              .reduce((value, key) => value?.[key], raw) !== undefined,
        )
        .map((suffix) => `/phases/${index}${suffix}`),
    );
    return {
      id: `obs-${String(number).padStart(2, '0')}`,
      source: 'local-postgresql-capture',
      summary: JSON.stringify({
        provenance: 'local-postgresql-measurement',
        source: { captureId },
        window: { start: baseline.window.start, end: incident.window.end },
        resource,
        units,
        records,
      }),
    };
  };
  /** Retain measured effective settings while excluding connection URLs and secrets. */
  const configRecords = (item) => ({
    config: pick(item.config, [
      'timestamp',
      'event',
      'pool_size',
      'max_overflow',
      'pool_timeout_seconds',
      'statement_timeout_ms',
    ]),
  });
  /** Associate a capture with its measured immutable source identity. */
  const sourceRecords = (item) => ({ source: source(item.raw) });
  /** Preserve each request's SQL counts and timing inside its measured window. */
  const sqlRecords = (item) => ({
    operations: item.writes.map((write) => ({
      ...pick(write, ['started_at', 'completed_at']),
      ...operation(write),
    })),
  });
  /** Project connection lifetimes only for the already bounded request set. */
  const connectionRecords = (item) => ({ requests: item.writes.map(lifetime) });
  /** Project outcomes only for the already bounded request set. */
  const resultRecords = (item) => ({ requests: item.writes.map(result) });
  /** Refuse source excerpts whose hashes do not match the measured revision. */
  const code = (number, module) =>
    wrap(
      number,
      (item) => {
        const snippet = snippets[item.context];
        assert.ok(snippet, `missing ${module} source snippet`);
        assert.equal(
          snippet.sha256,
          item.raw.source.files[`revision/${module}.py`],
        );
        return { source: pick(snippet, ['path', 'sha256', 'lines']) };
      },
      ['/source/files'],
    );

  let observations;
  if (caseName === 'pool') {
    observations = [
      wrap(1, resultRecords, ['/writes']),
      wrap(2, (item) => ({ ...configRecords(item), ...sourceRecords(item) }), [
        '/events',
        '/source',
      ]),
      wrap(3, connectionRecords, ['/writes']),
      wrap(4, sqlRecords, ['/writes']),
      wrap(
        5,
        (item) => ({
          concurrency: item.raw.concurrency,
          batch_rows: item.raw.batch_rows,
          request_ids: item.writes.map((write) => write.operation.request_id),
        }),
        ['/concurrency', '/batch_rows', '/writes'],
      ),
      wrap(
        6,
        (item) => ({ snapshot: snapshot(item.raw.snapshot_during_writes) }),
        ['/snapshot_during_writes'],
      ),
    ];
  } else if (caseName === 'query') {
    observations = [
      wrap(
        1,
        (item) => ({
          input: pick(item.raw.query_input, [
            'patient_id',
            'limit',
            'requests',
            'reading_type',
            'from_ts',
            'to_ts',
          ]),
          requests: item.writes.map((write) => ({
            ...result(write),
            sql_count: write.operation.sql_count,
          })),
        }),
        ['/query_input', '/queries'],
      ),
      code(2, 'query'),
      wrap(3, sqlRecords, ['/queries']),
      wrap(4, sourceRecords, ['/source']),
      wrap(
        5,
        (item) => ({ ...configRecords(item), ...connectionRecords(item) }),
        ['/events', '/queries'],
      ),
      wrap(6, connectionRecords, ['/queries']),
    ];
  } else if (caseName === 'lock') {
    observations = [
      wrap(1, resultRecords, ['/write', '/baseline_write', '/blocked_write']),
      wrap(
        2,
        (item) =>
          item.context === 'incident'
            ? {
                maintenance: maintenance(item.raw.maintenance),
                snapshot: snapshot(item.raw.snapshot),
              }
            : { requests: item.writes.map(result) },
        ['/maintenance', '/snapshot', '/write'],
      ),
      wrap(3, sourceRecords, ['/source']),
      wrap(4, sqlRecords, ['/write', '/baseline_write', '/blocked_write']),
      wrap(5, connectionRecords, [
        '/write',
        '/baseline_write',
        '/blocked_write',
      ]),
      wrap(
        6,
        (item) => ({ ...configRecords(item), ...connectionRecords(item) }),
        ['/events'],
      ),
    ];
  } else {
    observations = [
      wrap(1, resultRecords, [
        '/baseline_write',
        '/probes',
        '/subsequent_write',
      ]),
      wrap(2, connectionRecords, ['/probes']),
      code(3, 'session'),
      wrap(4, (item) => ({ ...sourceRecords(item), ...configRecords(item) }), [
        '/source',
        '/events',
      ]),
      wrap(
        5,
        (item) => ({ ...configRecords(item), ...connectionRecords(item) }),
        ['/events', '/probes'],
      ),
      wrap(6, (item) => ({ snapshot: snapshot(item.raw.snapshot) }), [
        '/snapshot',
      ]),
    ];
  }
  return { observations, operatorMapping, cutoff: incident.window.end };
}
