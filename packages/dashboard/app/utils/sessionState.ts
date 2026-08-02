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
 * stages, CC Headless collapses them into a single ANALYZING — so this module
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
  OUTDATED: '만료됨',
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
    'CC Headless 엔진이 프롬프트 주도로 읽기 전용 RCA·보고서 전문 에이전트를 자율 실행 중인 상태',
  COMPLETED:
    'RCA 분석이 정상 완료되어 보고서가 S3에 저장되고 알림이 발송된 상태',
  FAILED: '파이프라인 실행 중 오류가 발생하여 분석이 중단된 상태',
  CANCELLED: '사용자가 대시보드에서 수동으로 분석을 중단한 상태',
  OUTDATED: 'TTL 만료 등으로 더 이상 유효하지 않은 세션',
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

export function isTerminalState(state: string): boolean {
  return (TERMINAL_STATES as readonly string[]).includes(state);
}

export function stateLabel(state: string): string {
  return STATE_LABEL[state] || state;
}
