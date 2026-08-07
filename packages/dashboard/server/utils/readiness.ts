/**
 * Whether a finished analysis is waiting for a person, and if not, why not.
 *
 * `COMPLETED` only says the analysis stopped. It does not say whether anything
 * can be done about it — that needs three facts the approval endpoint already
 * checks: the analysis finished, the report carries execution steps, and no
 * execution is already running. The session list showed only the first, so a
 * report sitting unapproved and a report already resolved both read '완료' and
 * the one thing a person could act on was invisible.
 *
 * These conditions mirror what publishing an approval enforces. They are derived
 * here rather than in the page so both the list and the report answer the same
 * way — a row that offers approval the server would refuse is worse than no
 * offer at all.
 */

export const READINESS = [
  /** Analysis finished, steps exist, nothing running — a person's move. */
  'AWAITING_APPROVAL',
  /** An execution is running or already terminal. Read that state instead. */
  'EXECUTION_UNDERWAY',
  /** Finished, but no confirmed cause means no procedure to approve. */
  'NO_PROCEDURE',
  /** The analysis itself did not finish. */
  'NOT_COMPLETED',
] as const;

export type Readiness = (typeof READINESS)[number];

export const READINESS_LABEL: Record<Readiness, string> = {
  AWAITING_APPROVAL: '승인 대기',
  EXECUTION_UNDERWAY: '실행 진행',
  NO_PROCEDURE: '절차 없음',
  NOT_COMPLETED: '미완료',
};

export function readinessOf({
  state,
  stepCount,
  hasExecution,
}: {
  state: string;
  stepCount: number;
  hasExecution: boolean;
}): Readiness {
  if (state !== 'COMPLETED') return 'NOT_COMPLETED';
  // Any execution at all — running or finished — means approval already happened
  // once, so this row is no longer waiting on a decision.
  if (hasExecution) return 'EXECUTION_UNDERWAY';
  if (stepCount <= 0) return 'NO_PROCEDURE';
  return 'AWAITING_APPROVAL';
}
