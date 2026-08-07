import assert from 'node:assert/strict';
import path from 'node:path';
import test from 'node:test';
import { pathToFileURL } from 'node:url';

import { REPOSITORY_ROOT } from './evaluator.mjs';

/**
 * What a session row claims about an incident.
 *
 * `COMPLETED` only says the analysis stopped. It says nothing about whether
 * anyone approved the resulting procedure, and for a while the dashboard showed
 * ten reports awaiting a person's decision as plain '완료' — indistinguishable
 * from the ninety that needed nothing. These tests hold the two apart: the
 * readiness a report is in, and the single outcome word a reader sees.
 *
 * They also hold the causal chain's parse, because the report page leads with the
 * chain and both engines write it as prose rather than as data.
 */
async function importModule(relativePath) {
  return import(pathToFileURL(path.join(REPOSITORY_ROOT, relativePath)).href);
}

const {
  outcomeOf,
  OUTCOME_LABEL,
  OUTCOME_TONE,
  READINESS_LABEL,
  needsAttention,
  stoppedAtLabel,
  engineTrack,
  STATE_LABEL,
} = await importModule('packages/dashboard/app/utils/sessionState.ts');

const { readinessOf, READINESS } = await importModule(
  'packages/dashboard/server/utils/readiness.ts',
);

const { countExecutionSteps } = await importModule(
  'packages/dashboard/server/utils/playbook.ts',
);

const { furthestStage } = await importModule(
  'packages/dashboard/server/utils/progress.ts',
);

const { parseCausalChain, parseTimeline } = await importModule(
  'packages/dashboard/app/utils/causalChain.ts',
);

test('a finished analysis with steps and no execution is awaiting a person', () => {
  // The regression this exists for: this row read '완료' and offered nothing.
  assert.equal(
    readinessOf({ state: 'COMPLETED', stepCount: 4, hasExecution: false }),
    'AWAITING_APPROVAL',
  );
  assert.equal(
    outcomeOf({ state: 'COMPLETED', readiness: 'AWAITING_APPROVAL' }),
    'AWAITING',
  );
  assert.ok(needsAttention('AWAITING'), 'awaiting approval is somebody’s move');
});

test('a finished analysis with no confirmed cause is not awaiting anything', () => {
  // No steps means there is nothing a person could approve, so offering approval
  // here would promise what the server refuses with a 409.
  assert.equal(
    readinessOf({ state: 'COMPLETED', stepCount: 0, hasExecution: false }),
    'NO_PROCEDURE',
  );
  assert.equal(
    outcomeOf({ state: 'COMPLETED', readiness: 'NO_PROCEDURE' }),
    'NO_CAUSE',
  );
  assert.ok(!needsAttention('NO_CAUSE'));
});

test('an executed report stops asking for approval', () => {
  assert.equal(
    readinessOf({ state: 'COMPLETED', stepCount: 4, hasExecution: true }),
    'EXECUTION_UNDERWAY',
  );

  // Once an execution exists, the incident is described by what the execution
  // did — not by the analysis having finished.
  assert.equal(
    outcomeOf({
      state: 'COMPLETED',
      readiness: 'EXECUTION_UNDERWAY',
      executionState: 'RESOLVED',
    }),
    'RESOLVED',
  );
  for (const failed of ['UNRESOLVED', 'FAILED', 'CANCELLED']) {
    assert.equal(
      outcomeOf({
        state: 'COMPLETED',
        readiness: 'EXECUTION_UNDERWAY',
        executionState: failed,
      }),
      'UNRESOLVED',
      `${failed} execution must not read as resolved`,
    );
  }
  assert.ok(!needsAttention('RESOLVED'));
});

test('an unfinished or skipped analysis is never approvable', () => {
  for (const state of ['ANALYZING', 'SCOPING', 'FAILED', 'OUTDATED']) {
    assert.equal(
      readinessOf({ state, stepCount: 4, hasExecution: false }),
      'NOT_COMPLETED',
      `${state} is not a completed analysis`,
    );
  }
  assert.equal(outcomeOf({ state: 'EVIDENCE_COLLECTION' }), 'RUNNING');
  assert.equal(outcomeOf({ state: 'FAILED' }), 'BROKEN');
  assert.equal(outcomeOf({ state: 'CANCELLED' }), 'BROKEN');
  assert.equal(outcomeOf({ state: 'OUTDATED' }), 'SKIPPED');
});

test('a retrospective revision decides how many steps are approvable', () => {
  const step = (stepId) => ({
    step_id: stepId,
    action: `run ${stepId}`,
    success_criteria: `${stepId} succeeds`,
  });
  const session = {
    SK: 'strands#SESSION',
    confirmed: true,
    playbook_id: 'current',
    playbook_span_id: 'abc',
  };
  const playbookSpan = {
    SK: 'strands#SPAN#abc',
    engine: 'strands',
    span_type: 'PLAYBOOK',
    metadata: {
      playbook_id: 'current',
      execution_steps: [step('a'), step('b')],
    },
  };
  assert.equal(countExecutionSteps([session, playbookSpan], 'strands'), 2);

  // The revision is what the next execution runs, so it wins over what analysis
  // first recorded — counting the stale steps would offer a procedure that no
  // longer exists.
  const revision = {
    SK: 'strands#PLAYBOOK_REVISION',
    playbook_id: 'current',
    playbook: JSON.stringify({
      playbook_id: 'current',
      execution_steps: [step('a'), step('b'), step('c')],
    }),
  };
  assert.equal(
    countExecutionSteps([session, playbookSpan, revision], 'strands'),
    3,
  );

  // Approval rejects the whole procedure when any step is incomplete; readiness
  // must not offer a partially executable playbook that approval would refuse.
  const unnamed = {
    SK: 'strands#SPAN#abc',
    engine: 'strands',
    span_type: 'PLAYBOOK',
    metadata: {
      playbook_id: 'current',
      execution_steps: [step('a'), { intent: 'no id' }],
    },
  };
  assert.equal(countExecutionSteps([session, unnamed], 'strands'), 0);

  // One engine's playbook must not be counted for the other's row.
  assert.equal(countExecutionSteps([session, playbookSpan], 'cc-headless'), 0);
  assert.equal(countExecutionSteps([], 'strands'), 0);
});

test('a stopped run says which stage it stopped at', () => {
  // FAILED alone cannot tell a near-complete analysis from one that never began.
  const early = stoppedAtLabel('strands', 'SCOPING');
  const late = stoppedAtLabel('strands', 'REPORT_GENERATION');
  assert.notEqual(early, late);
  assert.match(early, /스코핑/);
  assert.match(late, /보고서 생성/);
  assert.match(late, /7\/8단계/);

  // Nothing recorded, or a stage off this engine's track, admits to nothing
  // rather than inventing a position.
  assert.equal(stoppedAtLabel('strands', ''), '');
  assert.equal(stoppedAtLabel('cc-headless', 'EVIDENCE_COLLECTION'), '');
});

test('the furthest span decides the stage, whatever order the spans arrive in', () => {
  const spans = [
    { spanType: 'REPORT', engine: 'strands' },
    { spanType: 'SCOPING', engine: 'strands' },
    { spanType: 'EVIDENCE_COLLECTION', engine: 'strands' },
  ];
  assert.equal(furthestStage(spans, 'strands'), 'REPORT_GENERATION');
  assert.equal(
    furthestStage([...spans].reverse(), 'strands'),
    'REPORT_GENERATION',
  );

  // Bookkeeping spans wrap a stage rather than being one.
  assert.equal(
    furthestStage(
      [
        { spanType: 'TERMINATION', engine: 'strands' },
        { spanType: 'BRANCHING', engine: 'strands' },
      ],
      'strands',
    ),
    '',
  );
  assert.equal(furthestStage([], 'strands'), '');

  // CC Headless runs the analysis as one stage, so any work means ANALYZING —
  // not a Strands stage name it never enters.
  assert.equal(
    furthestStage(
      [{ spanType: 'REPORT', engine: 'cc-headless' }],
      'cc-headless',
    ),
    'ANALYZING',
  );
});

test('every outcome and readiness a row can hold has a Korean label and a tone', () => {
  const outcomes = [
    'RUNNING',
    'AWAITING',
    'RESOLVED',
    'UNRESOLVED',
    'NO_CAUSE',
    'BROKEN',
    'SKIPPED',
  ];
  for (const outcome of outcomes) {
    assert.ok(OUTCOME_LABEL[outcome], `${outcome} has a label`);
    assert.ok(OUTCOME_TONE[outcome], `${outcome} has a tone`);
  }
  for (const readiness of READINESS) {
    assert.ok(READINESS_LABEL[readiness], `${readiness} has a label`);
  }
  for (const engine of ['strands', 'cc-headless']) {
    for (const stage of engineTrack(engine)) {
      assert.ok(STATE_LABEL[stage], `${stage} has a label`);
    }
  }

  // The skipped-for-age state used to read '만료됨', which described a TTL sweep
  // rather than the intake decision the engines actually make.
  assert.doesNotMatch(STATE_LABEL.OUTDATED, /만료/);
});

test('the causal chain survives both engines’ prose, and never half-parses', () => {
  // Strands separates with '→', CC Headless with '—'; the report page leads with
  // this chain, so a drift in either would leave the page with nothing to show.
  const strands = parseCausalChain(
    [
      '## 5 Whys',
      '- 1. Why 수집이 실패했는가? → 커넥션을 얻지 못했다',
      '- 2. Why 얻지 못했는가? → 풀이 고갈됐다',
      '',
      '## Timeline',
    ].join('\n'),
  );
  assert.equal(strands.length, 2);
  assert.equal(strands[0].index, 1);
  assert.match(strands[0].question, /수집이 실패했는가/);
  assert.doesNotMatch(strands[0].question, /\?$/);
  assert.match(strands[1].answer, /풀이 고갈/);

  const ccHeadless = parseCausalChain(
    ['## 5 Whys', '1. 왜 초과했는가? — `pool` 이 상승했다', '', '## 다음'].join(
      '\n',
    ),
  );
  assert.equal(ccHeadless.length, 1);
  // Rendered as text, so inline Markdown must not reach the screen literally.
  assert.doesNotMatch(ccHeadless[0].answer, /[`*]/);

  // A line with no separator is prose around the chain, not a link in it: half a
  // link on screen is worse than none.
  const partial = parseCausalChain(
    ['## 5 Whys', '- 이건 그냥 설명 문장이다', '## 끝'].join('\n'),
  );
  assert.deepEqual(partial, []);

  // No section at all, or no report, yields nothing rather than throwing.
  assert.deepEqual(parseCausalChain('## Root Cause\n원인'), []);
  assert.deepEqual(parseCausalChain(''), []);
  assert.deepEqual(parseCausalChain(null), []);
});

test('the timeline keeps only lines a clock reading anchors', () => {
  const moments = parseTimeline(
    [
      '## Timeline',
      '- 05:40:02 UTC: 배포 실행',
      '- 05:41~05:42 UTC: 임계값 초과',
      '- 조사 단계: 가설 5개 수립 → 검증',
      '## 다음',
    ].join('\n'),
  );
  assert.equal(moments.length, 2);
  assert.equal(moments[0].time, '05:40:02');
  assert.equal(moments[0].event, '배포 실행');
  // A Korean range stays a range; it is not a strikethrough and not two times.
  assert.equal(moments[1].time, '05:41~05:42');

  assert.deepEqual(parseTimeline('## 증거 시간 범위\n- 없음'), []);
  assert.deepEqual(parseTimeline(null), []);
});
