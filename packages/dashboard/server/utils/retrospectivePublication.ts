import type { PublicationAttempt } from '../../shared/types/retrospective-publication.ts';

type Row = Record<string, unknown>;
const statuses = new Set([
  'WAITING_FOR_PUBLICATION',
  'PUBLISHING',
  'PUBLISHED',
  'BLOCKED',
  'FAILED',
]);
const hash = (value: unknown): value is string =>
  typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
const nonempty = (value: unknown): value is string =>
  typeof value === 'string' && value.trim().length > 0;
const epoch = (value: unknown): value is number =>
  typeof value === 'number' && Number.isSafeInteger(value) && value > 0;

/** Attach only live children of this exact approved execution; invalid children never imply legacy success. */
export function readPublicationHistory(
  execution: Row,
  rows: Row[],
  now = Date.now() / 1000,
) {
  const prefix = `RETROSPECTIVE_FOLLOWUP#${execution.execution_id}#`;
  const candidates = rows.filter(
    (row) => typeof row.SK === 'string' && row.SK.startsWith(prefix),
  );
  const publicationAttempts: PublicationAttempt[] = [];
  let unavailable = false;
  for (const row of candidates) {
    if (
      execution.execution_state !== 'RESOLVED' ||
      execution.source_part !== 'recovery' ||
      !hash(execution.playbook_digest) ||
      !hash(execution.source_part_revision) ||
      execution.source_part_revision !== execution.source_part_payload_sha256 ||
      row.source_part_revision !== execution.source_part_revision ||
      !execution.approval_id ||
      !epoch(execution.ttl) ||
      execution.ttl <= now ||
      row.PK !== execution.PK ||
      row.PK !== `RCA#${execution.rca_id}` ||
      row.rca_id !== execution.rca_id ||
      row.execution_id !== execution.execution_id ||
      row.engine !== execution.engine ||
      row.approval_id !== execution.approval_id ||
      row.playbook_digest !== execution.playbook_digest ||
      row.record_type !== 'RETROSPECTIVE_FOLLOWUP' ||
      row.schema_version !== 1 ||
      row.review_status !== 'COMPLETED' ||
      !hash(row.attempt_id) ||
      !hash(row.review_sha256) ||
      row.attempt_id !== row.review_sha256 ||
      row.SK !== `${prefix}${row.attempt_id}` ||
      !statuses.has(String(row.status)) ||
      (row.status === 'PUBLISHED' &&
        (!nonempty(row.public_playbook_id) ||
          !nonempty(row.published_revision))) ||
      typeof row.reason !== 'string' ||
      !epoch(row.created_at) ||
      !epoch(row.updated_at) ||
      !epoch(row.ttl) ||
      row.updated_at < row.created_at ||
      row.ttl <= now ||
      row.created_at >= row.ttl
    ) {
      unavailable = true;
      continue;
    }
    publicationAttempts.push({
      attemptId: row.attempt_id,
      reviewStatus: 'COMPLETED',
      status: row.status as PublicationAttempt['status'],
      reason: row.reason,
      createdAt: row.created_at,
      updatedAt: row.updated_at,
      expiresAt: row.ttl,
      ...(row.status === 'PUBLISHED'
        ? {
            publicPlaybookId: row.public_playbook_id as string,
            publishedRevision: row.published_revision as string,
          }
        : {}),
    });
  }
  publicationAttempts.sort(
    (a, b) =>
      b.createdAt - a.createdAt ||
      b.updatedAt - a.updatedAt ||
      b.attemptId.localeCompare(a.attemptId),
  );
  return {
    publicationAttempts,
    publicationReadState: unavailable
      ? ('UNAVAILABLE' as const)
      : candidates.length
        ? ('AVAILABLE' as const)
        : ('NOT_RECORDED' as const),
  };
}
