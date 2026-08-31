/**
 * The vocabulary the dashboard uses to describe an RCA session's state.
 *
 * Three views render these states — the session list, the trace page and the
 * state graph — and every one of them needs the same label for the same state.
 * Held separately they drift, and the drift is invisible: each view reads
 * correct on its own while telling the operator something different about the
 * same session. That already happened once, with the graph still describing a
 * conditional-remediation step the analysis engines no longer perform.
 *
 * The state names themselves are the engines' contract, not this module's. Both
 * engines write their own transitions — Strands moves through the nine pipeline
 * stages, the headless engines collapse them into a single ANALYZING — so this module
 * only names what the engines record and must not be treated as the source of
 * which transitions are legal.
 */

export const STATE_LABEL: Record<string, string> = {
  ALARM_RECEIVED: '알람 수신',
  SCOPING: '스코핑',
  HYPOTHESIS_GENERATION: '가설 생성',
  HYPOTHESIS_PRIORITIZATION: '우선순위 결정',
  EVIDENCE_COLLECTION: '증거 수집',
  HYPOTHESIS_VALIDATION: '가설 검증',
  REPORT_GENERATION: '보고서 생성',
  ANALYZING: '분석 중',
  COMPLETED: '완료',
  FAILED: '실패',
  CANCELLED: '중단됨',
  // 'Expired' read as a TTL sweep. The alarm was skipped for being too old to
  // analyse, which is a decision made at intake, not a record aging out.
  OUTDATED: '스킵됨',
};

export const STATE_DESC: Record<string, string> = {
  ALARM_RECEIVED:
    'CloudWatch 알람이 SNS→SQS 경로로 수신되어 RCA 세션이 생성된 초기 상태',
  SCOPING:
    '알람 메트릭과 관련 메트릭을 조회하여 영향범위와 심각도를 판단하는 단계',
  HYPOTHESIS_GENERATION:
    '스코핑 결과를 바탕으로 3~5개의 근본원인 가설을 생성하는 단계',
  HYPOTHESIS_PRIORITIZATION:
    '생성된 가설의 우선순위를 결정하고 상위 빔을 선택하는 단계',
  EVIDENCE_COLLECTION:
    'CloudWatch, CloudTrail, GitHub 등에서 가설 검증을 위한 증거를 수집하는 단계',
  HYPOTHESIS_VALIDATION:
    '수집된 증거를 바탕으로 가설을 채택(CONFIRMED), 기각(REJECTED), 또는 추가 조사(NEEDS_INVESTIGATION)로 분류하는 단계. 매 루프 진입 시 Accepted Review Gate가 기존 채택 가설을 리뷰하여 중복 탐색을 차단한다.',
  REPORT_GENERATION:
    '플레이북을 포함한 한글 RCA 보고서를 생성하는 단계. 분석은 여기서 끝나고 복구는 수행하지 않는다.',
  // 분석은 읽기 전용이다. 복구는 사용자가 플레이북 실행을 승인한 뒤 쓰기 권한을 가진
  // 별도 에이전트가 수행하므로, 이 단계의 설명이 복구를 암시하면 안 된다.
  ANALYZING:
    'Headless Codex 엔진이 프롬프트 주도로 읽기 전용 RCA·보고서 전문 에이전트를 자율 실행 중인 상태',
  COMPLETED:
    'RCA 분석이 정상 완료되어 보고서가 S3에 저장되고 알림이 발송된 상태',
  FAILED: '파이프라인 실행 중 오류가 발생하여 분석이 중단된 상태',
  CANCELLED: '사용자가 대시보드에서 수동으로 분석을 중단한 상태',
  // 판정 근거는 TTL이 아니라 알람 나이다. 기준은 엔진마다 다르다 — 예산 소진 한 번이
  // 뒤따르는 알람을 폐기하지 않으려면 기준이 그 엔진의 시간 예산 이상이어야 한다.
  OUTDATED:
    '알람이 너무 오래되어 분석에 들어가지 않고 종료된 상태. 첫 수신 시점에 알람 나이가 기준(Strands 30분, Headless Codex 60분)을 넘으면 이 상태가 된다.',
};

/**
 * States no engine writes out of.
 *
 * Mirrors the terminal set both engines enforce on their session records, and
 * the one the cancel/delete fencing conditions check server-side.
 */
export const TERMINAL_STATES = [
  'COMPLETED',
  'FAILED',
  'CANCELLED',
  'OUTDATED',
] as const;

/**
 * The stages a run passes through, per engine, in order.
 *
 * Used to say how far a stopped run got: a terminal state overwrites the stage it
 * happened in, so '3단계 중 스코핑에서' has to be derived from this order rather
 * than read off the session. The two engines have genuinely different lengths
 * (Strands moves through the pipeline stage by stage, headless engines collapse them
 * into a single autonomous run), and flattening them to a common length would
 * claim a precision neither engine reports.
 */
export const ENGINE_TRACK: Record<string, readonly string[]> = {
  strands: [
    'ALARM_RECEIVED',
    'SCOPING',
    'HYPOTHESIS_GENERATION',
    'HYPOTHESIS_PRIORITIZATION',
    'EVIDENCE_COLLECTION',
    'HYPOTHESIS_VALIDATION',
    'REPORT_GENERATION',
    'COMPLETED',
  ],
  'headless-codex': ['ALARM_RECEIVED', 'ANALYZING', 'COMPLETED'],
  'codex-headless': ['ALARM_RECEIVED', 'ANALYZING', 'COMPLETED'],
  'cc-headless': ['ALARM_RECEIVED', 'ANALYZING', 'COMPLETED'],
};

export function engineTrack(engine: string): readonly string[] {
  return ENGINE_TRACK[engine] ?? ENGINE_TRACK.strands!;
}

export function isTerminalState(state: string): boolean {
  return (TERMINAL_STATES as readonly string[]).includes(state);
}

export function stateLabel(state: string): string {
  return STATE_LABEL[state] || state;
}

/**
 * What a person can still do about a finished run.
 *
 * `COMPLETED` says the analysis stopped, not whether anything remains. The server
 * derives these from the same three conditions the approval endpoint enforces,
 * and the labels live here so the list and the report name the state identically
 * — a row promising an approval the server would refuse is worse than no promise.
 */
export const READINESS_LABEL: Record<string, string> = {
  AWAITING_APPROVAL: '승인 대기',
  EXECUTION_UNDERWAY: '실행됨',
  NO_PROCEDURE: '절차 없음',
  NOT_COMPLETED: '미완료',
};

export const READINESS_DESC: Record<string, string> = {
  AWAITING_APPROVAL:
    '분석이 끝나고 실행할 절차도 있지만 아직 아무도 승인하지 않았다. 사람이 절차를 읽고 승인해야 실행이 시작된다.',
  EXECUTION_UNDERWAY:
    '이 리포트로 실행이 한 번 이상 발행되었다. 실행 자체의 결과는 실행 상태로 따로 읽는다.',
  NO_PROCEDURE:
    '분석은 끝났지만 근본원인이 확정되지 않아 실행할 절차가 없다. 추가 조사가 필요하다.',
  NOT_COMPLETED: '분석이 완료되지 않아 승인 대상이 아니다.',
};

/**
 * The one outcome word for a session row.
 *
 * A session has two independent lifecycles — the analysis and any execution of
 * its playbook — and showing both as equal badges made '완료' mean four different
 * situations. This collapses them into the single thing the reader needs: what
 * became of this incident, and whether it is still on someone's desk.
 */
export type Outcome =
  | 'RUNNING'
  | 'AWAITING'
  | 'RESOLVED'
  | 'UNRESOLVED'
  | 'NO_CAUSE'
  | 'BROKEN'
  | 'SKIPPED';

export interface OutcomeInput {
  state: string;
  readiness?: string;
  executionState?: string;
}

export function outcomeOf({
  state,
  readiness = '',
  executionState = '',
}: OutcomeInput): Outcome {
  if (!isTerminalState(state)) return 'RUNNING';
  if (state === 'OUTDATED') return 'SKIPPED';
  if (state === 'FAILED' || state === 'CANCELLED') return 'BROKEN';

  // A completed analysis is described by what happened after it, since the
  // analysis finishing is not itself an outcome for the incident.
  if (executionState === 'RESOLVED') return 'RESOLVED';
  if (
    executionState === 'UNRESOLVED' ||
    executionState === 'FAILED' ||
    executionState === 'CANCELLED'
  ) {
    return 'UNRESOLVED';
  }
  if (readiness === 'AWAITING_APPROVAL') return 'AWAITING';
  if (readiness === 'NO_PROCEDURE') return 'NO_CAUSE';
  return 'AWAITING';
}

export const OUTCOME_LABEL: Record<Outcome, string> = {
  RUNNING: '분석 중',
  AWAITING: '승인 대기',
  RESOLVED: '해결',
  UNRESOLVED: '미해결',
  NO_CAUSE: '원인 미확정',
  BROKEN: '분석 중단',
  SKIPPED: '건너뜀',
};

/**
 * Operational state colours shared by the queue and detail header.
 *
 * Visible labels remain mandatory: colour accelerates scanning but never carries
 * the meaning alone.
 */
export const OUTCOME_TONE: Record<Outcome, string> = {
  RUNNING: 'text-info',
  AWAITING: 'text-warning',
  RESOLVED: 'text-success',
  UNRESOLVED: 'text-error mark-broken',
  NO_CAUSE: 'text-warning',
  BROKEN: 'text-error mark-broken',
  SKIPPED: 'text-base-content/48',
};

/** Outcomes that put a session on somebody's desk. */
export function needsAttention(outcome: Outcome): boolean {
  return outcome === 'RUNNING' || outcome === 'AWAITING';
}

/**
 * Where a stopped run got to, in words.
 *
 * A terminal state overwrites the stage it happened in, so a run that died
 * generating the report and one that died on its first metric call both read
 * FAILED. `stoppedAt` carries the furthest stage the spans recorded, and saying
 * it — '보고서 생성에서 멈춤' — is the difference between a near-complete analysis
 * and one that never started. Returns '' when the spans recorded nothing, since
 * inventing a stage would be worse than admitting there is none.
 */
export function stoppedAtLabel(engine: string, stoppedAt: string): string {
  if (!stoppedAt) return '';
  const track = engineTrack(engine);
  const at = track.indexOf(stoppedAt);
  if (at < 0) return '';
  const label = STATE_LABEL[stoppedAt] || stoppedAt;
  return `${label}에서 멈춤 · ${at + 1}/${track.length}단계`;
}
