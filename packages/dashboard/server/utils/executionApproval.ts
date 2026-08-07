import { createHash } from 'node:crypto';

type DataRecord = Record<string, unknown>;

export const APPROVAL_REQUESTED_BY = 'dashboard';
export const APPROVAL_TTL_DAYS = 90;

export interface ExecutionRequestFields {
  execution_id: string;
  rca_id: string;
  engine: string;
  approval_id: string;
  requested_by: string;
  report_s3_key: string;
  approved_playbook_s3_key: string;
  playbook_digest: string;
}

function canonicalize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalize);
  if (value !== null && typeof value === 'object') {
    const record = value as DataRecord;
    return Object.fromEntries(
      Object.keys(record)
        .sort()
        .map((key) => [key, canonicalize(record[key])]),
    );
  }
  return value;
}

export function serializePlaybookSnapshot(playbook: DataRecord): Uint8Array {
  return Buffer.from(JSON.stringify(canonicalize(playbook)), 'utf8');
}

export function sha256Hex(bytes: Uint8Array): string {
  return createHash('sha256').update(bytes).digest('hex');
}

export function isUuid(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
    value,
  );
}

export function executionReservationMatches(
  item: DataRecord | undefined,
  request: ExecutionRequestFields,
): boolean {
  if (!item || item.execution_state !== 'PENDING_APPROVAL') return false;
  if (item.attempt !== 0) return false;
  return Object.entries(request).every(([field, expected]) => {
    return item[field] === expected;
  });
}
