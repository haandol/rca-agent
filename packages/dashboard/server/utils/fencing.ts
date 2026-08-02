/**
 * The ownership rules an operator action has to satisfy before it touches a
 * session.
 *
 * Cancel and delete are started by a person rather than by a worker, but they
 * write to the same session record the running engine owns. Left outside the
 * claim rules, they undo the fencing that keeps a redelivered message from
 * running alongside a live execution — so both go through the conditions built
 * here, and both fail closed when the condition does not hold.
 *
 * Both engines write the same ownership attributes (`claim_token`,
 * `side_effect_lease_expires_at`), so one set of rules covers them.
 */

export const TERMINAL_SESSION_STATES = [
  'COMPLETED',
  'FAILED',
  'OUTDATED',
  'CANCELLED',
] as const;

const STATE_PLACEHOLDERS: Record<string, string> = {
  COMPLETED: ':completed',
  FAILED: ':failed',
  OUTDATED: ':outdated',
  CANCELLED: ':cancelled',
};

const TERMINAL_STATE_LIST = TERMINAL_SESSION_STATES.map(
  (state) => STATE_PLACEHOLDERS[state],
).join(', ');

/**
 * True while a side effect the worker started is still inside its lease.
 *
 * An external write already in flight cannot be cut in half — stopping it
 * mid-way leaves the outcome unknown — so an operator action waits for the lease
 * to expire or be released instead of interrupting it.
 */
const NO_ACTIVE_LEASE =
  '(attribute_not_exists(side_effect_lease_expires_at) OR side_effect_lease_expires_at < :nowEpoch)';

export interface FencedUpdate {
  UpdateExpression: string;
  ConditionExpression: string;
  ExpressionAttributeNames: Record<string, string>;
  ExpressionAttributeValues: Record<string, unknown>;
}

function terminalStateValues(): Record<string, string> {
  return Object.fromEntries(
    TERMINAL_SESSION_STATES.map((state) => [STATE_PLACEHOLDERS[state], state]),
  );
}

/**
 * Cancels a session by rotating its claim in the same write.
 *
 * Setting the state alone would leave the running execution holding a claim it
 * still considers current, so it would keep recording hypotheses and trace spans
 * and would finish side effects already inside a lease. Rotating the claim token
 * is what makes the cancel take effect: every later write by that execution
 * checks `claim_token` and now fails.
 *
 * The rotated token is a fresh value, so cancel does not need to know the old
 * one — any value that differs fences the previous owner.
 */
export function buildCancelUpdate(
  fencedClaimToken: string,
  now: string,
  nowEpochSeconds: number,
): FencedUpdate {
  return {
    UpdateExpression:
      'SET #st = :cancelled, claim_token = :fenced, updated_at = :now, cancelled_at = :now',
    ConditionExpression: [
      'attribute_exists(PK)',
      `NOT #st IN (${TERMINAL_STATE_LIST})`,
      NO_ACTIVE_LEASE,
    ].join(' AND '),
    ExpressionAttributeNames: { '#st': 'state' },
    ExpressionAttributeValues: {
      ...terminalStateValues(),
      ':fenced': fencedClaimToken,
      ':now': now,
      ':nowEpoch': nowEpochSeconds,
    },
  };
}

/**
 * Claims a session for deletion, refusing anything still live.
 *
 * Deleting an active session removes the very record the fencing is based on:
 * the running execution never learns its claim is gone and keeps going, while a
 * redelivered message sees no session and starts over — the two run at once.
 * So deletion is allowed only from a terminal state with no active lease, and an
 * operator has to cancel first otherwise.
 *
 * This is an update rather than a plain precondition read because the check and
 * the removal have to be one step. Rotating the claim here means that if the
 * check passes, no stale writer can resurrect the record between this write and
 * the deletes that follow.
 */
export function buildDeleteClaimUpdate(
  fencedClaimToken: string,
  now: string,
  nowEpochSeconds: number,
): FencedUpdate {
  return {
    UpdateExpression:
      'SET claim_token = :fenced, updated_at = :now, deleting_at = :now',
    ConditionExpression: [
      'attribute_exists(PK)',
      `#st IN (${TERMINAL_STATE_LIST})`,
      NO_ACTIVE_LEASE,
    ].join(' AND '),
    ExpressionAttributeNames: { '#st': 'state' },
    ExpressionAttributeValues: {
      ...terminalStateValues(),
      ':fenced': fencedClaimToken,
      ':now': now,
      ':nowEpoch': nowEpochSeconds,
    },
  };
}

/** A claim value that tells an operator reading the record why writes fail. */
export function fencedClaimToken(reason: 'cancelled' | 'deleted'): string {
  return `${reason}:${globalThis.crypto.randomUUID()}`;
}

export function isConditionalCheckFailure(error: unknown): boolean {
  return (
    (error as { name?: string })?.name === 'ConditionalCheckFailedException'
  );
}
