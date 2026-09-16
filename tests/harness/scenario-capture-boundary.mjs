// Dataset projection only: no evaluator policy, model execution, or DB calls.
// The original proof and these selectors belong to operators, never to prompts.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';

const HISTORICAL_CASES = {
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
  if (scenarioId === 'write-column-regression') {
    return projectWriteColumnCaptures(proof, snippets);
  }
  // Historical callers only: retained raw captures still reproduce their old projection.
  if (proof.kind === 'aws-maintenance-capture') {
    assert.equal(scenarioId, 'maintenance-transaction-lock');
    return projectAwsMaintenanceCaptures(proof, snippets);
  }
  const caseName = HISTORICAL_CASES[scenarioId];
  assert.ok(caseName, 'unknown catalog id');
  const baseline = phaseData(proof, caseName, 'normal');
  const incident = phaseData(proof, caseName, 'fault');
  const pair = [baseline, incident];
  const resource = { runId: proof.run_id, schema: proof.schema };
  const operatorMapping = {};
  const ordinal = Object.keys(HISTORICAL_CASES).indexOf(scenarioId) + 1;
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

/**
 * Project this recorded AWS incident, never an operator's recovery verdict.
 * files contains archived source bytes; hashes are checked before any selection.
 * Collection time may follow the cutoff only for timestamped historical events.
 */
export function projectAwsMaintenanceCaptures(capture, files) {
  assert.equal(capture.kind, 'aws-maintenance-capture');
  const cutoff = iso(capture.incidentCutoff, 'incident cutoff');
  const cutoffMs = Date.parse(cutoff);
  const raw = {};
  for (const [key, descriptor] of Object.entries(capture.files)) {
    assert.ok(Object.hasOwn(files, key), `missing capture ${key}`);
    assert.equal(
      createHash('sha256').update(files[key]).digest('hex'),
      descriptor.sha256,
      `capture hash mismatch: ${key}`,
    );
    raw[key] = key.endsWith('Source')
      ? files[key].toString()
      : JSON.parse(files[key].toString());
  }
  assert.equal(raw.analysisStart[0].createdAt, cutoff);

  const beforeCutoff = (value, label) => {
    assert.equal(typeof value, 'string', `missing timestamp: ${label}`);
    const timestamp = Date.parse(value);
    assert.ok(
      Number.isFinite(timestamp) && timestamp <= cutoffMs,
      `after cutoff: ${label}`,
    );
    return value;
  };
  const exactlyOne = (items, predicate, label) => {
    const matches = items.filter(predicate);
    assert.equal(matches.length, 1, `missing or ambiguous ${label}`);
    return matches[0];
  };
  const journalData = (key, kind) => {
    assert.equal(raw[key].kind, kind);
    beforeCutoff(raw[key].at, key);
    return raw[key].data;
  };
  const baseline = journalData('baseline', 'snapshot');
  const definition = journalData('definition', 'maintenance_revision').response
    .taskDefinition;
  const statuses = [
    journalData('statusBefore', 'status'),
    journalData('statusIncident', 'status'),
  ];
  const runId = capture.runId;
  assert.equal(baseline.options.run_id, runId);
  assert.ok(runId);
  const containerName = baseline.options.container;
  const region = baseline.target.region;
  const cluster = baseline.target.cluster;
  const schema = baseline.options.maintenance_schema;
  const events = (document, eventName) =>
    document.events
      .filter(
        (event) =>
          Number.isFinite(event.timestamp) && event.timestamp <= cutoffMs,
      )
      .map((event) => ({ envelope: event, record: JSON.parse(event.message) }))
      .filter(({ record }) => record.event === eventName);
  const oneEvent = (document, eventName) =>
    exactlyOne(events(document, eventName), () => true, eventName);
  const acquired = oneEvent(raw.maintenanceLogs, 'maintenance_lock_acquired');
  const identity = oneEvent(raw.maintenanceLogs, 'ecs_runtime_identity');
  const installed = oneEvent(raw.maintenanceLogs, 'source_manifest');
  const lock = acquired.record;
  assert.equal(lock.run_id, runId);
  beforeCutoff(lock.acquired_at, 'lock acquisition');
  assert.ok(
    Date.parse(lock.expires_at) > cutoffMs,
    'maintenance already expired at cutoff',
  );
  assert.equal(identity.record.status, 'available');
  assert.equal(identity.record.Cluster, cluster);
  assert.equal(definition.family, identity.record.Family);
  assert.equal(String(definition.revision), identity.record.Revision);
  beforeCutoff(definition.registeredAt, 'task definition registration');

  const taskContainer = (task) =>
    exactlyOne(
      task.containers,
      (item) => item.name === containerName,
      'application container',
    );
  const tags = (items) => {
    assert.equal(
      new Set(items.map((item) => item.key)).size,
      items.length,
      'duplicate ownership tag',
    );
    return Object.fromEntries(items.map((item) => [item.key, item.value]));
  };
  const ownedTasks = statuses.map((status) => {
    assert.equal(status.runId, runId);
    const task = exactlyOne(
      status.maintenanceTasks,
      (item) => item.taskArn === identity.record.TaskARN,
      'owned maintenance task',
    );
    assert.equal(task.lastStatus, 'RUNNING');
    assert.equal(task.desiredStatus, 'RUNNING');
    assert.equal(task.clusterArn, cluster);
    assert.equal(task.taskDefinitionArn, definition.taskDefinitionArn);
    assert.equal(task.group, `family:${definition.family}`);
    assert.equal(tags(task.tags).RealisticDemoRunId, runId);
    assert.equal(tags(task.tags).RealisticDemoJournal, raw.baseline.hash);
    assert.equal(task.startedBy, `demo-${raw.baseline.hash.slice(0, 32)}`);
    beforeCutoff(task.startedAt, 'task start');
    return task;
  });
  const jobContainer = exactlyOne(
    definition.containerDefinitions,
    (item) => item.name === containerName,
    'maintenance definition container',
  );
  assert.deepEqual(jobContainer.entryPoint, ['python']);
  assert.deepEqual(jobContainer.command, [
    '-m',
    'test_service.maintenance',
    '--run-id',
    runId,
    '--hold-seconds',
    String(Number(lock.hold_seconds).toFixed(1)),
    '--schema',
    schema,
  ]);
  assert.equal(jobContainer.essential, true);
  assert.equal(jobContainer.image, baseline.image);
  for (const task of ownedTasks) {
    assert.equal(taskContainer(task).image, jobContainer.image);
    assert.equal(taskContainer(task).imageDigest, baseline.image.split('@')[1]);
  }
  const appTask = exactlyOne(
    baseline.tasks,
    (task) => task.taskDefinitionArn === baseline.service.taskDefinition,
    'baseline service task',
  );
  const appSources = exactlyOne(
    baseline.sourceManifests,
    (source) => source.taskArn === appTask.taskArn,
    'service source capture',
  );
  const appInstalled = oneEvent(appSources, 'source_manifest');
  assert.equal(
    appSources.imageDigest,
    taskContainer(ownedTasks[0]).imageDigest,
  );
  for (const manifest of [installed.record, appInstalled.record]) {
    assert.equal(manifest.verified, true);
    assert.equal(manifest.revision, 'r1');
    for (const key of ['maintenanceSource', 'sessionSource']) {
      assert.equal(
        manifest.files[capture.sourceExcerpts[key].path],
        capture.files[key].sha256,
      );
    }
  }
  assert.equal(installed.record.fingerprint, appInstalled.record.fingerprint);
  for (const status of statuses) {
    assert.equal(
      status.service.taskDefinition,
      baseline.service.taskDefinition,
    );
    assert.equal(status.service.clusterArn, cluster);
    assert.equal(status.service.serviceArn, baseline.target.service);
    const task = exactlyOne(
      status.tasks,
      (item) => item.taskArn === appTask.taskArn,
      'same service task',
    );
    assert.equal(task.taskDefinitionArn, appTask.taskDefinitionArn);
    assert.equal(
      taskContainer(task).imageDigest,
      taskContainer(appTask).imageDigest,
    );
  }

  const dbEvents = events(raw.blockers, 'db_wait_snapshot').sort(
    (left, right) => left.envelope.timestamp - right.envelope.timestamp,
  );
  assert.ok(dbEvents.length >= 2, 'missing DB wait/return observations');
  for (const event of dbEvents) {
    assert.equal(event.envelope.logStreamName, appSources.logStreamName);
    for (const row of event.record.activity) {
      if (row.xact_start !== null)
        beforeCutoff(row.xact_start, 'DB transaction');
      beforeCutoff(row.query_start, 'DB query');
    }
  }
  const blocked = exactlyOne(
    dbEvents,
    ({ record }) =>
      record.activity.some((row) =>
        row.blocking_pids.includes(lock.backend_pid),
      ),
    'blocked DB observation',
  );
  const owner = exactlyOne(
    blocked.record.activity,
    (row) => row.pid === lock.backend_pid,
    'DB owner',
  );
  assert.equal(owner.application_name, `healthcare-maint-${runId}`);
  assert.equal(
    Date.parse(owner.xact_start),
    Date.parse(lock.transaction_start),
  );
  const waiter = exactlyOne(
    blocked.record.activity,
    (row) => row.blocking_pids.includes(lock.backend_pid),
    'blocked application backend',
  );
  assert.equal(waiter.wait_event_type, 'Lock');
  assert.equal(waiter.wait_event, 'relation');
  assert.ok(
    blocked.record.locks.some(
      (row) =>
        row.pid === lock.backend_pid &&
        row.mode === 'ShareLock' &&
        row.granted &&
        row.relation === 'sensor_readings',
    ),
  );
  assert.ok(
    blocked.record.locks.some(
      (row) =>
        row.pid === waiter.pid &&
        row.mode === 'RowExclusiveLock' &&
        !row.granted &&
        row.relation === 'sensor_readings',
    ),
  );
  const returned = exactlyOne(
    dbEvents,
    (event) =>
      event.envelope.timestamp > blocked.envelope.timestamp &&
      event.record.activity.some(
        (row) =>
          row.pid === waiter.pid &&
          row.state === 'idle' &&
          row.xact_start === null,
      ),
    'same backend after its request',
  );
  assert.equal(returned.record.pool_checked_out, 0);
  assert.ok(
    returned.record.locks.some(
      (row) =>
        row.pid === lock.backend_pid && row.mode === 'ShareLock' && row.granted,
    ),
  );

  // This later API response may supply a historical alarm transition, never its later task state.
  const alarm = exactlyOne(
    raw.alarmStatus.data.alarms,
    (item) =>
      item.MetricName === 'VitalIngestFailures' && item.StateValue === 'ALARM',
    'observed ingest alarm',
  );
  beforeCutoff(alarm.StateTransitionedTimestamp, 'alarm transition');
  beforeCutoff(alarm.StateUpdatedTimestamp, 'alarm update');
  const baselineAlarm = exactlyOne(
    baseline.alarms,
    (item) => item.AlarmArn === alarm.AlarmArn,
    'baseline alarm',
  );
  assert.equal(raw.analysisStart[0].alarmName, alarm.AlarmName);
  const alarmConfigKeys = [
    'AlarmName',
    'AlarmArn',
    'AlarmDescription',
    'MetricName',
    'Namespace',
    'Dimensions',
    'Statistic',
    'Period',
    'EvaluationPeriods',
    'DatapointsToAlarm',
    'Threshold',
    'ComparisonOperator',
    'TreatMissingData',
  ];
  assert.deepEqual(
    pick(alarm, alarmConfigKeys),
    pick(baselineAlarm, alarmConfigKeys),
  );
  const alarmReason = JSON.parse(alarm.StateReasonData);
  beforeCutoff(alarmReason.queryDate, 'alarm evaluation');
  for (const point of alarmReason.evaluatedDatapoints) {
    assert.ok(
      Date.parse(point.timestamp) + alarm.Period * 1000 <= cutoffMs,
      'incomplete alarm metric period',
    );
  }
  const alarmPayload = {
    name: alarm.AlarmName,
    description: alarm.AlarmDescription,
    metric: alarm.MetricName,
    stateReason: alarm.StateReason,
    stateChangeTime: alarm.StateTransitionedTimestamp,
    region,
    arn: alarm.AlarmArn,
    namespace: alarm.Namespace,
    dimensions: Object.fromEntries(
      alarm.Dimensions.map((item) => [item.Name, item.Value]),
    ),
    statistic: alarm.Statistic,
    period: alarm.Period,
    evaluationPeriods: alarm.EvaluationPeriods,
    threshold: alarm.Threshold,
    comparisonOperator: alarm.ComparisonOperator,
    treatMissingData: alarm.TreatMissingData,
    ...(Object.hasOwn(alarm, 'DatapointsToAlarm')
      ? { datapointsToAlarm: alarm.DatapointsToAlarm }
      : {}),
  };

  const metricRecords = (metrics) =>
    Object.fromEntries(
      [
        'VitalIngestAttempts',
        'VitalIngestFailures',
        'PatientVitalsQueryDuration',
      ].map((name) => {
        const series = metrics.values[name];
        const points = series.Datapoints.filter(
          (point) => Date.parse(point.Timestamp) + 60_000 <= cutoffMs,
        ).sort(
          (left, right) =>
            Date.parse(left.Timestamp) - Date.parse(right.Timestamp),
        );
        assert.ok(
          points.length > 0,
          `missing complete metric periods: ${name}`,
        );
        return [
          name,
          {
            Label: series.Label,
            periodSeconds: 60,
            Datapoints: points.map((point) =>
              pick(point, [
                'Timestamp',
                'SampleCount',
                'Average',
                'Sum',
                'Unit',
              ]),
            ),
          },
        ];
      }),
    );
  const taskView = (task) => ({
    ...pick(task, [
      'taskArn',
      'clusterArn',
      'taskDefinitionArn',
      'startedBy',
      'startedAt',
      'group',
      'lastStatus',
      'tags',
    ]),
    container: pick(taskContainer(task), ['name', 'image', 'imageDigest']),
  });
  const eventView = ({ envelope, record }) => ({
    ...pick(envelope, ['timestamp', 'logStreamName', 'eventId']),
    record: snapshot(record),
  });
  const sourceView = (key) => {
    const excerpt = capture.sourceExcerpts[key];
    const lines = raw[key].split('\n');
    return {
      path: excerpt.path,
      sha256: capture.files[key].sha256,
      lines: excerpt.ranges.flatMap(([start, end]) =>
        lines
          .slice(start - 1, end)
          .map((text, offset) => ({ number: start + offset, text }))
          .filter(
            ({ text }) =>
              !text.trim().startsWith('#') && !text.trim().startsWith('"""'),
          ),
      ),
    };
  };
  const manifestView = (manifest) => ({
    ...pick(manifest, ['revision', 'verified', 'fingerprint']),
    files: pick(manifest.files, [
      'maintenance.py',
      'revision/query.py',
      'revision/session.py',
    ]),
  });
  const settings = pick(baseline.environment, [
    'DB_POOL_SIZE',
    'DB_MAX_OVERFLOW',
    'DB_POOL_TIMEOUT_SECONDS',
    'DB_STATEMENT_TIMEOUT_MS',
    'DB_OBSERVABILITY_ENABLED',
    'DB_OBSERVABILITY_INTERVAL_SECONDS',
  ]);
  assert.equal(settings.DB_POOL_SIZE.value, String(blocked.record.pool_size));
  const operatorMapping = {};
  const wrap = (number, records, pointers) => {
    const captureId = `capture-2${String(number).padStart(3, '0')}`;
    operatorMapping[captureId] = pointers;
    return {
      id: `obs-${String(number).padStart(2, '0')}`,
      source: 'aws-incident-capture',
      summary: JSON.stringify({
        provenance: 'aws-incident-measurement',
        source: { captureId },
        window: { start: baseline.metrics.start, end: cutoff },
        resource: {
          runId,
          schema,
          region,
          cluster,
          service: baseline.target.service,
        },
        units: {
          timestamp: 'Unix milliseconds',
          Timestamp: 'ISO-8601 timestamp with recorded offset',
          periodSeconds: 'Seconds',
          pool_checked_out: 'Count',
          pool_size: 'Count',
          database_connections: 'Count',
          server_max_connections: 'Count',
        },
        records,
      }),
    };
  };
  const observations = [
    wrap(
      1,
      [
        { context: 'baseline', metrics: metricRecords(baseline.metrics) },
        {
          context: 'incident',
          metrics: metricRecords(statuses[1].metrics),
          alarm: {
            ...pick(alarm, [
              ...alarmConfigKeys,
              'StateValue',
              'StateReason',
              'StateTransitionedTimestamp',
            ]),
            evaluation: alarmReason,
          },
        },
      ],
      [
        'baseline#/data/metrics',
        'statusIncident#/data/metrics',
        'alarmStatus#/data/alarms',
      ],
    ),
    wrap(
      2,
      [
        {
          context: 'incident',
          maintenance: maintenance(lock),
          identity: pick(identity.record, [
            'TaskARN',
            'Cluster',
            'Family',
            'Revision',
          ]),
          snapshot: eventView(blocked),
          ownedTask: taskView(ownedTasks[0]),
          observedAt: raw.statusBefore.at,
        },
      ],
      [
        'maintenanceLogs#/events',
        'blockers#/events/0',
        'statusBefore#/data/maintenanceTasks',
      ],
    ),
    wrap(
      3,
      [
        {
          context: 'baseline',
          observedAt: raw.baseline.at,
          serviceTask: taskView(appTask),
          source: manifestView(appInstalled.record),
        },
        {
          context: 'incident',
          observedAt: raw.statusIncident.at,
          ownedTask: taskView(ownedTasks[1]),
          taskDefinition: pick(definition, [
            'taskDefinitionArn',
            'family',
            'revision',
            'registeredAt',
          ]),
          container: pick(jobContainer, [
            'name',
            'image',
            'entryPoint',
            'command',
            'essential',
          ]),
          source: manifestView(installed.record),
          sourceExcerpt: sourceView('maintenanceSource'),
        },
      ],
      [
        'baseline#/data/sourceManifests',
        'baseline#/data/tasks',
        'statusIncident#/data/maintenanceTasks',
        'definition#/data/response/taskDefinition',
        'maintenanceLogs#/events/0',
        'maintenanceSource#',
      ],
    ),
    wrap(
      4,
      [
        {
          context: 'incident',
          observations: [eventView(blocked), eventView(returned)],
          missingMeasurements: [
            'per-request SQL duration',
            'statement outcome',
          ],
        },
      ],
      ['blockers#/events/0', 'blockers#/events/1'],
    ),
    wrap(
      5,
      [
        {
          context: 'incident',
          observations: [eventView(blocked), eventView(returned)],
          sourceExcerpt: sourceView('sessionSource'),
          missingMeasurements: ['per-request checkout/checkin counts'],
        },
      ],
      ['blockers#/events/0', 'blockers#/events/1', 'sessionSource#'],
    ),
    wrap(
      6,
      [
        {
          context: 'baseline',
          observedAt: raw.baseline.at,
          taskDefinitionArn: baseline.service.taskDefinition,
          settings,
        },
        {
          context: 'incident',
          observedAt: raw.statusIncident.at,
          taskDefinitionArn: statuses[1].service.taskDefinition,
          poolObservations: [blocked, returned].map(({ envelope, record }) => ({
            timestamp: envelope.timestamp,
            ...pick(record, [
              'pool_checked_out',
              'pool_size',
              'database_connections',
              'server_max_connections',
            ]),
          })),
        },
      ],
      [
        'baseline#/data/environment',
        'statusIncident#/data/service/taskDefinition',
        'blockers#/events',
      ],
    ),
  ];
  return { observations, operatorMapping, cutoff, alarm: alarmPayload };
}

/** Project a fresh native-column proof; restoration and operator verdicts never enter input. */
export function projectWriteColumnCaptures(proof, snippets) {
  assert.equal(proof.boundary, 'local_postgresql_service');
  assert.ok(proof.run_id && proof.schema);
  const phases = ['normal', 'fault'].map((phase) => {
    const matches = proof.phases.filter((item) => item.phase === phase);
    const windows = proof.phase_windows.filter(
      (item) => item.phase === phase && item.case !== 'setup',
    );
    assert.equal(matches.length, 1);
    assert.equal(windows.length, 1);
    const raw = matches[0];
    const window = {
      start: iso(windows[0].started_at, 'phase start'),
      end: iso(windows[0].completed_at, 'phase end'),
    };
    assert.ok(Date.parse(window.start) < Date.parse(window.end));
    assert.equal(raw.source.verified, true);
    assert.match(raw.source.files['revision/write.py'], /^[a-f0-9]{64}$/);
    // Python source manifests use sorted default-separator JSON, unlike the baseline wire.
    const fileMap =
      '{' +
      Object.entries(raw.source.files)
        .sort()
        .map(
          ([key, value]) => `${JSON.stringify(key)}: ${JSON.stringify(value)}`,
        )
        .join(', ') +
      '}';
    const fingerprint = createHash('sha256').update(fileMap).digest('hex');
    assert.equal(raw.source.fingerprint, fingerprint);
    const context = phase === 'normal' ? 'baseline' : 'incident';
    const snippet = snippets[context];
    assert.equal(snippet.path, 'revision/write.py');
    assert.equal(
      createHash('sha256').update(snippet.text).digest('hex'),
      raw.source.files[snippet.path],
    );
    const allowed = [
      'event',
      'observed_at',
      'request_id',
      'operation',
      'sqlstate',
      'error_type',
      'schema_name',
      'table_name',
      'driver_table_name',
      'driver_column_name',
      'column_names',
      'sql_hash',
      'sql_hash_algorithm',
      'count',
      'completion_semantics',
    ];
    const events = raw.events
      .filter((event) =>
        [
          'db_write_error',
          'db_schema_snapshot',
          'write_completed',
          'write_contract',
        ].includes(event.event),
      )
      .map((event) => {
        const stamp = iso(event.observed_at, 'logger observation');
        assert.ok(
          Date.parse(stamp) >= Date.parse(window.start) &&
            Date.parse(stamp) <= Date.parse(window.end),
        );
        return pick(event, allowed);
      });
    assert.ok(events.some((event) => event.event === 'db_schema_snapshot'));
    assert.ok(
      events.some(
        (event) =>
          event.event ===
          (phase === 'normal' ? 'write_completed' : 'db_write_error'),
      ),
    );
    return {
      context,
      window,
      source: pick(raw.source, [
        'revision',
        'fingerprint',
        'base_fingerprint',
        'files',
      ]),
      code: { path: snippet.path, text: snippet.text },
      events,
      service: pick(raw, [
        'write_statuses',
        'rows_before',
        'rows_after',
        'read_status',
        'alerts_status',
        'health',
        'checked_out',
        'metrics',
      ]),
    };
  });
  assert.ok(
    Date.parse(phases[0].window.end) <= Date.parse(phases[1].window.start),
  );
  assert.ok(phases[0].source.base_fingerprint);
  assert.equal(
    phases[0].source.base_fingerprint,
    phases[1].source.base_fingerprint,
  );
  assert.deepEqual(
    Object.keys(phases[0].source.files).sort(),
    Object.keys(phases[1].source.files).sort(),
  );
  assert.deepEqual(
    Object.keys(phases[0].source.files).filter(
      (key) => phases[0].source.files[key] !== phases[1].source.files[key],
    ),
    ['revision/write.py'],
  );
  phases.forEach((phase) => {
    phase.connections = { checked_out: phase.service.checked_out };
  });
  const observations = [
    'events',
    'source',
    'code',
    'service',
    'connections',
  ].map((field, index) => ({
    id: `obs-0${index + 1}`,
    source: 'local-postgresql-capture',
    summary: JSON.stringify({
      provenance: 'local-postgresql-measurement',
      resource: { runId: proof.run_id, schema: proof.schema },
      records: phases.map((phase) => ({
        context: phase.context,
        window: phase.window,
        [field]: phase[field],
      })),
    }),
  }));
  return {
    observations,
    cutoff: phases[1].window.end,
    operatorMapping: Object.fromEntries(
      observations.map((item, index) => [
        item.id,
        ['normal', 'fault'].map(
          (phase) =>
            `/phases/${proof.phases.findIndex((p) => p.phase === phase)}/${['events', 'source', 'source/files', 'write_statuses', 'checked_out'][index]}`,
        ),
      ]),
    ),
  };
}
