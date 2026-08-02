import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';
import { pathToFileURL } from 'node:url';

import { REPOSITORY_ROOT } from './evaluator.mjs';

// Cancel and delete are the two writes a person makes to a session a worker
// owns. Left outside the claim rules they undo the fencing that keeps a
// redelivered message from running beside a live execution, so these tests pin
// the conditions rather than the wording of the handlers around them.
const {
  buildCancelUpdate,
  buildDeleteClaimUpdate,
  fencedClaimToken,
  TERMINAL_SESSION_STATES,
} = await import(
  pathToFileURL(
    path.join(REPOSITORY_ROOT, 'packages/dashboard/server/utils/fencing.ts'),
  ).href
);

// Which sort keys hold a session is part of the key layout, not of the fencing
// conditions — the fencing only decides what an operator may write to one.
const { isSessionSortKey } = await import(
  pathToFileURL(
    path.join(REPOSITORY_ROOT, 'packages/dashboard/server/utils/keys.ts'),
  ).href
);

async function readRepositoryFile(relativePath) {
  return readFile(path.join(REPOSITORY_ROOT, relativePath), 'utf8');
}

const NOW = '2026-07-31T00:00:00.000Z';
const NOW_EPOCH = 1785196800;

test('cancelling rotates the claim in the same write that sets the state', () => {
  const update = buildCancelUpdate('cancelled:abc', NOW, NOW_EPOCH);

  // Setting the state alone leaves the execution holding a claim it still thinks
  // is current, so it keeps recording hypotheses and trace spans after the
  // cancel returns. The rotation has to be in this write, not a second one — a
  // separate write could lose the race it is meant to win.
  assert.match(update.UpdateExpression, /#st = :cancelled/);
  assert.match(update.UpdateExpression, /claim_token = :fenced/);
  assert.equal(update.ExpressionAttributeValues[':fenced'], 'cancelled:abc');
  assert.equal(update.ExpressionAttributeNames['#st'], 'state');
});

test('cancelling refuses a terminal session', () => {
  const update = buildCancelUpdate('cancelled:abc', NOW, NOW_EPOCH);

  // A completed analysis must not be walked back to CANCELLED: the report it
  // produced is already published and readable.
  assert.match(update.ConditionExpression, /NOT #st IN \(/);
  for (const state of TERMINAL_SESSION_STATES) {
    assert.ok(
      Object.values(update.ExpressionAttributeValues).includes(state),
      `${state} is not guarded against`,
    );
  }
});

test('cancelling refuses while a side effect still holds its lease', () => {
  const update = buildCancelUpdate('cancelled:abc', NOW, NOW_EPOCH);

  // An external write already inside a lease cannot be cut in half — stopping it
  // mid-way leaves the outcome unknown — so the cancel waits for the lease to
  // lapse instead of interrupting it.
  assert.match(
    update.ConditionExpression,
    /attribute_not_exists\(side_effect_lease_expires_at\) OR side_effect_lease_expires_at < :nowEpoch/,
  );
  assert.equal(update.ExpressionAttributeValues[':nowEpoch'], NOW_EPOCH);
});

test('deleting is allowed only from a terminal state with no active lease', () => {
  const update = buildDeleteClaimUpdate('deleted:abc', NOW, NOW_EPOCH);

  // Deleting an active session removes the record the fencing is based on: the
  // running execution never learns its claim is gone, and a redelivered message
  // sees no session and starts over. Both then run at once.
  assert.match(update.ConditionExpression, /#st IN \(/);
  assert.doesNotMatch(update.ConditionExpression, /NOT #st IN \(/);
  assert.match(
    update.ConditionExpression,
    /attribute_not_exists\(side_effect_lease_expires_at\) OR side_effect_lease_expires_at < :nowEpoch/,
  );
});

test('the delete claim fences the record before the deletes that follow', () => {
  const update = buildDeleteClaimUpdate('deleted:abc', NOW, NOW_EPOCH);

  // Checking and removing have to be one step: without rotating the claim here,
  // a stale writer could resurrect the record between the check and the delete.
  assert.match(update.UpdateExpression, /claim_token = :fenced/);
  assert.equal(update.ExpressionAttributeValues[':fenced'], 'deleted:abc');
  assert.doesNotMatch(update.UpdateExpression, /#st =/);
});

test('a fenced claim token cannot collide with a live one', () => {
  const first = fencedClaimToken('cancelled');
  const second = fencedClaimToken('cancelled');

  assert.notEqual(first, second);
  // The reason travels with the token so an operator reading the record can see
  // why the worker's writes started failing.
  assert.match(first, /^cancelled:/);
  assert.match(fencedClaimToken('deleted'), /^deleted:/);
});

test('session records are recognised across engines and the legacy layout', () => {
  assert.ok(isSessionSortKey('SESSION'));
  assert.ok(isSessionSortKey('strands#SESSION'));
  assert.ok(isSessionSortKey('cc-headless#SESSION'));
  assert.ok(!isSessionSortKey('strands#HYPO#h1'));
  assert.ok(!isSessionSortKey('EXEC#e1'));
});

test('the cancel handler fences instead of only relabelling', async () => {
  const source = await readRepositoryFile(
    'packages/dashboard/server/api/sessions/[id]/cancel.post.ts',
  );

  assert.match(source, /buildCancelUpdate\(/);
  assert.match(source, /fencedClaimToken\('cancelled'\)/);
  // A refused cancel has to surface as a conflict; swallowing it would report a
  // stop that never happened.
  assert.match(source, /statusCode: 409/);
});

test('the delete handler refuses an active session or execution', async () => {
  const source = await readRepositoryFile(
    'packages/dashboard/server/api/sessions/[id].delete.ts',
  );

  assert.match(source, /buildDeleteClaimUpdate\(/);
  assert.match(source, /statusCode: 409/);
  // An execution is a separate lifecycle, but its records sit in the same
  // partition this delete clears and it checks its own claim on every write.
  assert.match(source, /inFlightExecutions\(/);
  assert.match(source, /isTerminalExecution\(/);

  // The fence must come before the deletes, or the check guards nothing.
  assert.ok(
    source.indexOf('buildDeleteClaimUpdate') <
      source.indexOf('BatchWriteCommand({'),
    'records are deleted before the session is fenced',
  );
});
