import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import test from 'node:test';

import { REPOSITORY_ROOT } from './evaluator.mjs';

async function readRepositoryFile(relativePath) {
  return readFile(path.join(REPOSITORY_ROOT, relativePath), 'utf8');
}

test('dashboard normalizes persisted session and span remediation contracts', async () => {
  const moduleUrl = pathToFileURL(
    path.join(
      REPOSITORY_ROOT,
      'packages/dashboard/server/utils/remediation.ts',
    ),
  ).href;
  const {
    mergeRemediationDetails,
    readSessionRemediation,
    readSpanRemediation,
  } = await import(moduleUrl);

  const strandsSession = {
    PK: 'RCA#rca-strands-1',
    SK: 'strands#SESSION',
    engine: 'strands',
    remediation_status: 'COMPLETED',
    remediation_success: false,
    remediation_summary: 'Reset request completed',
    remediation_error: 'Metric remained above threshold',
    remediation_completed_at: '2026-07-21T10:00:00Z',
    verification_status: 'FAILED',
    verification_summary: 'CPU is still elevated',
    verification_remaining_issues: ['CPUUtilization > 80'],
  };

  assert.deepEqual(readSessionRemediation(strandsSession), {
    remediationStatus: 'COMPLETED',
    remediationSuccess: false,
    remediationSummary: 'Reset request completed',
    remediationError: 'Metric remained above threshold',
    remediationCompletedAt: '2026-07-21T10:00:00Z',
    verificationStatus: 'FAILED',
    metricsNormalized: false,
    verificationSummary: 'CPU is still elevated',
    remainingIssues: ['CPUUtilization > 80'],
    remediationFaultType: '',
    remediationEndpoint: '',
  });

  const ccRemediationSpan = {
    PK: 'RCA#rca-cc-1',
    SK: 'cc-headless#SPAN#remediation-1',
    engine: 'cc-headless',
    span_type: 'REMEDIATION',
    span_status: 'COMPLETED',
    input_summary: 'Healthcare reset completed',
    end_time: '2026-07-21T10:01:00Z',
    metadata: {
      status: 'SUCCEEDED',
      fault_type: 'persistent_database_latency',
      endpoint_path: '/faults/database-latency/reset',
      verification: {
        status: 'NORMALIZED',
        reason: 'Alarm metric returned below threshold',
      },
    },
  };
  const ccRemediation = readSpanRemediation(ccRemediationSpan);

  assert.deepEqual(ccRemediation, {
    remediationStatus: 'SUCCEEDED',
    remediationSuccess: null,
    remediationSummary: 'Healthcare reset completed',
    remediationError: '',
    remediationCompletedAt: '2026-07-21T10:01:00Z',
    verificationStatus: 'NORMALIZED',
    metricsNormalized: true,
    verificationSummary: 'Alarm metric returned below threshold',
    remainingIssues: [],
    remediationFaultType: 'persistent_database_latency',
    remediationEndpoint: '/faults/database-latency/reset',
  });

  assert.deepEqual(
    mergeRemediationDetails(
      readSessionRemediation({
        PK: 'RCA#rca-cc-1',
        SK: 'cc-headless#SESSION',
        engine: 'cc-headless',
        state: 'COMPLETED',
      }),
      ccRemediation,
    ),
    ccRemediation,
  );

  assert.equal(
    readSessionRemediation({
      verification_status: 'NORMALIZED',
      metrics_normalized: false,
    }).metricsNormalized,
    false,
    'an explicit producer boolean takes precedence over the status fallback',
  );

  assert.deepEqual(
    mergeRemediationDetails(
      readSessionRemediation({
        remediation_status: 'COMPLETED',
        remediation_success: true,
        verification_status: 'NORMALIZED',
        verification_remaining_issues: [],
      }),
      readSpanRemediation({
        span_type: 'REMEDIATION',
        metadata: {
          status: 'FAILED',
          verification: {
            status: 'FAILED',
            remaining_issues: ['stale span issue'],
          },
        },
      }),
    ),
    {
      remediationStatus: 'COMPLETED',
      remediationSuccess: true,
      remediationSummary: '',
      remediationError: '',
      remediationCompletedAt: '',
      verificationStatus: 'NORMALIZED',
      metricsNormalized: true,
      verificationSummary: '',
      remainingIssues: [],
      remediationFaultType: '',
      remediationEndpoint: '',
    },
    'session-level Strands results remain authoritative over span fallback data',
  );

  assert.deepEqual(
    readSpanRemediation({
      span_type: 'REPORT',
      input_summary: 'This is not a remediation result',
      end_time: '2026-07-21T10:02:00Z',
    }),
    {
      remediationStatus: '',
      remediationSuccess: null,
      remediationSummary: '',
      remediationError: '',
      remediationCompletedAt: '',
      verificationStatus: '',
      metricsNormalized: null,
      verificationSummary: '',
      remainingIssues: [],
      remediationFaultType: '',
      remediationEndpoint: '',
    },
  );
});

test('dashboard APIs and UI consume authoritative remediation fields', async () => {
  const [
    sessionsSource,
    tracesSource,
    reportsSource,
    graphSource,
    detailSource,
    indexSource,
    tracePageSource,
  ] = await Promise.all([
    readRepositoryFile('packages/dashboard/server/api/sessions.get.ts'),
    readRepositoryFile('packages/dashboard/server/api/traces/[id].get.ts'),
    readRepositoryFile('packages/dashboard/server/api/reports/[id].get.ts'),
    readRepositoryFile('packages/dashboard/app/composables/useTraceGraph.ts'),
    readRepositoryFile(
      'packages/dashboard/app/components/flow/RemediationDetail.vue',
    ),
    readRepositoryFile('packages/dashboard/app/pages/index.vue'),
    readRepositoryFile('packages/dashboard/app/pages/trace/[id].vue'),
  ]);

  for (const field of [
    'remediationStatus',
    'remediationSummary',
    'remediationError',
    'verificationStatus',
    'metricsNormalized',
    'verificationSummary',
    'remainingIssues',
  ]) {
    assert.ok(graphSource.includes(field), `trace graph carries ${field}`);
  }

  // The APIs spread the normalized RemediationDetails rather than restating
  // each field, so assert the spread instead of individual field names.
  for (const [name, source] of [
    ['sessions', sessionsSource],
    ['trace', tracesSource],
  ]) {
    assert.match(
      source,
      /\.\.\.\w*[rR]emediation,/,
      `${name} API spreads the normalized remediation details`,
    );
  }
  assert.match(
    sessionsSource,
    /const remediation = readSessionRemediation\(item\)/,
    'sessions API derives remediation from the session record',
  );
  assert.match(
    tracesSource,
    /const remediation = readSpanRemediation\(i\)/,
    'trace API derives per-span remediation from the span record',
  );

  assert.match(detailSource, /node\.remediationStatus/);
  assert.match(detailSource, /node\.verificationStatus/);
  assert.match(detailSource, /node\.metricsNormalized/);
  assert.doesNotMatch(detailSource, /metadata\?\.verification/);
  assert.match(indexSource, /session\.remediationStatus/);
  assert.match(indexSource, /session\.verificationStatus/);
  assert.match(
    indexSource,
    /remediationBadgeClass\(\s*session\.remediationStatus,\s*session\.remediationSuccess,?\s*\)/,
  );
  assert.match(
    tracePageSource,
    /remediationBadgeClass\(\s*trace\.session\.remediationStatus,\s*trace\.session\.remediationSuccess,?\s*\)/,
  );
  assert.match(
    detailSource,
    /statusClass\(\s*detail\.status,\s*detail\.success,?\s*\)/,
  );

  assert.match(tracesSource, /mergeRemediationDetails/);
  assert.match(tracesSource, /span\.spanType === 'REMEDIATION'/);
  assert.match(tracesSource, /span\.engine === selectedSessionEngine/);

  const sessionLookup = reportsSource.indexOf('new GetCommand');
  const producerKey = reportsSource.indexOf('report_s3_key');
  const canonicalFallback = reportsSource.indexOf(
    '`reports/${engine}/${id}.md`',
  );
  const legacyFallback = reportsSource.indexOf('`reports/${id}.md`');
  assert.ok(
    sessionLookup >= 0,
    'report API queries the selected engine session',
  );
  assert.ok(producerKey > sessionLookup, 'report API reads report_s3_key');
  assert.ok(
    canonicalFallback > producerKey,
    'canonical report path follows the authoritative session key',
  );
  assert.ok(
    legacyFallback > canonicalFallback,
    'legacy report path is the final fallback',
  );
  assert.match(reportsSource, /ALLOWED_ENGINES\.has\(engineFilter\)/);
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
