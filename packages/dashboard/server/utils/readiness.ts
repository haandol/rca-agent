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

/**
 * How many execution steps a run's playbook declares.
 *
 * A retrospective revision supersedes what analysis first recorded, because the
 * revision is what the next execution would run. Counting the analysis-time
 * steps after a revision emptied or extended them would offer approval for a
 * procedure that no longer exists.
 *
 * The sort-key predicates are recognised inline rather than imported: every other
 * module under server/utils relies on Nitro's auto-import, and adding a real
 * import here would be the only one — while the offline contract tests load this
 * file directly, where no auto-import exists.
 */
export function countExecutionSteps(
  items: Record<string, unknown>[],
  engine: string,
): number {
  const revision = items.find(
    (item) => (item.SK as string) === `${engine}#PLAYBOOK_REVISION`,
  );
  if (revision) {
    const parsed = safeParseObject(revision.playbook);
    const steps = parsed?.execution_steps;
    if (Array.isArray(steps)) return steps.filter(hasStepId).length;
  }

  const span = items.find((item) => {
    const sortKey = (item.SK as string) || '';
    if (!sortKey.includes('SPAN#')) return false;
    if (item.span_type !== 'PLAYBOOK') return false;
    // Pre engine-split records use a bare `SPAN#` key and are always Strands.
    const owner =
      (item.engine as string) ||
      (sortKey.startsWith('SPAN#') ? 'strands' : (sortKey.split('#')[0] ?? ''));
    return owner === engine;
  });
  const metadata = span?.metadata as Record<string, unknown> | undefined;
  const steps = metadata?.execution_steps;
  return Array.isArray(steps) ? steps.filter(hasStepId).length : 0;
}

/**
 * A step with no id cannot be pointed at by evidence or corrected by a
 * retrospective, so it is not a step anyone can approve. This matches how the
 * playbook endpoint filters them out.
 */
function hasStepId(entry: unknown): boolean {
  if (entry === null || typeof entry !== 'object' || Array.isArray(entry))
    return false;
  const stepId = (entry as Record<string, unknown>).step_id;
  return typeof stepId === 'string' && stepId.trim().length > 0;
}

function safeParseObject(value: unknown): Record<string, unknown> | null {
  if (typeof value !== 'string') return null;
  try {
    const parsed = JSON.parse(value);
    return parsed !== null &&
      typeof parsed === 'object' &&
      !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}
