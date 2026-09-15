import assert from 'node:assert/strict';
import test from 'node:test';
import {
  readReportSummary,
  reportDisplayMarkdown,
} from '../../packages/dashboard/server/utils/reportSummary.ts';

const summary = {
  incident_summary: '요청이 지연됨',
  impact_summary: '수집 서비스 일부 요청',
  severity: 'high',
  root_cause: '연결 반환 누락',
  root_cause_confirmed: true,
  confidence_score: 0.94,
  next_action: '현재 런북 검토',
  runbook_verification_status: 'DRAFT',
  runbook_approval_eligible: true,
  playbook_id: 'pb',
  selected_playbook_id: 'prior',
  comparison_status: 'UPDATE_PROPOSED',
  proposal_state: 'PENDING',
};

function report(fields = summary) {
  return `# RCA\n\n## 빠른 판단\n<!-- rca-summary:v1\n${JSON.stringify(fields)}\n-->\n\n## Incident Summary\n원문은 전부 남는다.\n\n## Evidence\n원문 로그\n\n## Playbook\naws cloudwatch describe-alarms`;
}

test('the server marker yields a typed summary while all original detail remains available', () => {
  const markdown = report();
  assert.deepEqual(readReportSummary(markdown), {
    incidentSummary: '요청이 지연됨',
    impactSummary: '수집 서비스 일부 요청',
    severity: 'high',
    rootCause: '연결 반환 누락',
    confirmed: true,
    confidence: 0.94,
    nextAction: '현재 런북 검토',
    runbookVerificationStatus: 'DRAFT',
    runbookApprovalEligible: true,
    playbookId: 'pb',
    selectedPlaybookId: 'prior',
    comparisonStatus: 'UPDATE_PROPOSED',
    proposalState: 'PENDING',
  });
  const displayed = reportDisplayMarkdown(markdown);
  assert.doesNotMatch(displayed, /rca-summary:v1/);
  assert.ok(displayed.includes('원문은 전부 남는다.'));
  assert.ok(displayed.includes('원문 로그'));
  assert.ok(displayed.includes('aws cloudwatch describe-alarms'));
  assert.ok(
    markdown.includes('rca-summary:v1'),
    'the saved original was not changed',
  );
});

test('server verdict overrides report prose without turning the overview into permission', () => {
  const actual = readReportSummary(report(), {
    confirmed: false,
    root_cause: '아직 확정되지 않은 후보',
    confidence_score: 0.45,
  });
  assert.equal(actual.confirmed, false);
  assert.equal(actual.rootCause, '아직 확정되지 않은 후보');
  assert.equal(actual.confidence, 0.45);
  assert.equal(actual.runbookApprovalEligible, false);
});

test('legacy summaries use recorded sections and leave unavailable fields missing', () => {
  const actual = readReportSummary(
    '# Old report\n\n## Incident Summary\n원래 장애 설명\n\n- **Severity**: high\n\n## Impact Assessment\n기록된 영향\n\n## Root Cause\n과거 원인 후보\n\n- **Confidence**: 84%\n\n## Evidence\nconfidence: 1.0\n',
  );
  assert.equal(actual.incidentSummary, '원래 장애 설명');
  assert.equal(actual.impactSummary, '기록된 영향');
  assert.equal(actual.severity, 'high');
  assert.equal(actual.confidence, 0.84);
  assert.equal(actual.confirmed, null);
  assert.equal(actual.nextAction, null);
  assert.equal(actual.proposalState, null);
  assert.equal(
    readReportSummary(
      '## Evidence\nIncident Summary\nA quoted section name is not a heading.',
    ).incidentSummary,
    null,
  );
});

test('malformed, duplicate, and unsupported metadata cannot manufacture summary fields', () => {
  for (const markdown of [
    '<!-- rca-summary:v1\nnot json\n-->',
    `${report()}\n${report()}`,
    '<!-- rca-summary:v1\n[]\n-->',
  ]) {
    assert.equal(readReportSummary(markdown).confirmed, null);
    assert.equal(reportDisplayMarkdown(markdown), markdown);
  }
  const actual = readReportSummary(
    report({
      severity: 'urgent',
      confidence_score: 42,
      root_cause_confirmed: 'true',
      runbook_verification_status: 'trusted',
      comparison_status: 'APPROVED',
      proposal_state: 'executing',
    }),
  );
  assert.equal(actual.severity, null);
  assert.equal(actual.confidence, null);
  assert.equal(actual.confirmed, null);
  assert.equal(actual.runbookVerificationStatus, null);
  assert.equal(actual.comparisonStatus, null);
  assert.equal(actual.proposalState, null);
});

test('raw markup in summary remains data and machine delimiters cannot hide detail', () => {
  const fields = {
    ...summary,
    incident_summary: '<img src=x onerror=alert(1)>',
  };
  const encoded = JSON.stringify(fields)
    .replaceAll('<', '\\u003c')
    .replaceAll('>', '\\u003e');
  const markdown = `<!-- rca-summary:v1\n${encoded}\n-->\n## Evidence\n<script>must remain visible as source</script>`;
  assert.equal(
    readReportSummary(markdown).incidentSummary,
    fields.incident_summary,
  );
  assert.ok(reportDisplayMarkdown(markdown).includes('<script>'));
});
