type DataRecord = Record<string, unknown>;

/**
 * The execution lifecycle as the worker records it.
 *
 * Executions are separate items from the analysis session — a report can be
 * executed more than once and an execution failure must not make the analysis
 * look failed — so these fields are read from `EXEC#` items, never merged into
 * the session record.
 */
export const EXECUTION_STATES = [
  'PENDING_APPROVAL',
  'EXECUTING',
  'VERIFYING',
  'RESOLVED',
  'UNRESOLVED',
  'FAILED',
  'CANCELLED',
] as const;

export type ExecutionState = (typeof EXECUTION_STATES)[number];

const TERMINAL_STATES = new Set<ExecutionState>([
  'RESOLVED',
  'UNRESOLVED',
  'FAILED',
  'CANCELLED',
]);

export const EXECUTION_STATE_LABELS: Record<ExecutionState, string> = {
  PENDING_APPROVAL: '승인 대기',
  EXECUTING: '실행 중',
  VERIFYING: '검증 중',
  RESOLVED: '해결',
  UNRESOLVED: '미해결',
  FAILED: '실패',
  CANCELLED: '취소',
};

export interface ExecutionSummary {
  executionId: string;
  rcaId: string;
  engine: string;
  state: ExecutionState | 'UNKNOWN';
  stateLabel: string;
  approvalId: string;
  requestedBy: string;
  attempt: number;
  attemptedStepCount: number;
  blockedCount: number;
  failedStepCount: number;
  /** null when observation could not confirm resolution either way. */
  resolutionConfirmed: boolean | null;
  errorReason: string;
  evidenceS3Key: string;
  retrospectiveStatus: string;
  retrospectiveSummary: string;
  playbookSnapshotS3Key: string;
  retrospectiveDiffS3Key: string;
  createdAt: string;
  updatedAt: string;
}

function asRecord(value: unknown): DataRecord | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as DataRecord)
    : null;
}

function readString(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function readNumber(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

function readTristate(value: unknown): boolean | null {
  return typeof value === 'boolean' ? value : null;
}

export function parseExecutionState(
  value: unknown,
): ExecutionState | 'UNKNOWN' {
  return (EXECUTION_STATES as readonly string[]).includes(readString(value))
    ? (value as ExecutionState)
    : 'UNKNOWN';
}

export function isExecutionItem(sortKey: string): boolean {
  // Executions are not partitioned by engine: the execution path is the same
  // regardless of which engine produced the report it runs.
  return sortKey.startsWith(EXECUTION_SK_PREFIX);
}

export function isTerminalExecution(
  state: ExecutionState | 'UNKNOWN',
): boolean {
  return state !== 'UNKNOWN' && TERMINAL_STATES.has(state);
}

export function readExecution(item: DataRecord): ExecutionSummary {
  const sortKey = readString(item.SK);
  const state = parseExecutionState(item.execution_state);
  const summary = asRecord(item.evidence_summary) ?? {};

  return {
    executionId:
      readString(item.execution_id) || sortKey.replace(EXECUTION_SK_PREFIX, ''),
    rcaId: readString(item.rca_id) || rcaIdFromPk(readString(item.PK)),
    engine: readString(item.engine),
    state,
    stateLabel:
      state === 'UNKNOWN' ? '알 수 없음' : EXECUTION_STATE_LABELS[state],
    approvalId: readString(item.approval_id),
    requestedBy: readString(item.requested_by),
    attempt: readNumber(item.attempt) || 1,
    attemptedStepCount: readNumber(summary.attempted_step_count),
    blockedCount: readNumber(summary.blocked_count),
    failedStepCount: readNumber(summary.failed_step_count),
    resolutionConfirmed: readTristate(summary.resolution_confirmed),
    errorReason: readString(item.error_reason),
    evidenceS3Key: readString(item.evidence_s3_key),
    retrospectiveStatus: readString(item.retrospective_status),
    retrospectiveSummary: readString(item.retrospective_summary),
    playbookSnapshotS3Key: readString(item.playbook_snapshot_s3_key),
    retrospectiveDiffS3Key: readString(item.retrospective_diff_s3_key),
    createdAt: readString(item.created_at),
    updatedAt: readString(item.updated_at),
  };
}

/**
 * The execution a session list should show for a report.
 *
 * A report can be executed repeatedly, and what matters at a glance is where it
 * stands now — so the latest attempt wins rather than, say, the first success.
 */
export function latestExecution(
  executions: ExecutionSummary[],
): ExecutionSummary | null {
  if (!executions.length) return null;
  return [...executions].sort((a, b) => {
    if (a.attempt !== b.attempt) return b.attempt - a.attempt;
    return (b.updatedAt || '').localeCompare(a.updatedAt || '');
  })[0]!;
}
