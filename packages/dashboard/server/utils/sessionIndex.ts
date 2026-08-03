/**
 * The session-list index, and what reading it costs.
 *
 * Listing sessions by scanning the table read every span and hypothesis too and
 * threw them away: cost and latency tracked total trace volume rather than
 * session count, and a scan returns no ordering, so "the newest 25" could not be
 * asked for. This index answers both — newest first, sessions only.
 *
 * The keys are written on session records alone. Reusing `engine` and
 * `created_at`, which hypothesis and execution items also carry, would pull those
 * into the index; DynamoDB omits items missing an index key, so a key nothing
 * else writes is what makes the index session-only without a filter.
 */
export const SESSION_LIST_INDEX = 'session-by-engine-index';

/** The attribute whose presence means "this record is a session". */
export const LIST_PARTITION_KEY = 'list_engine';
export const LIST_SORT_KEY = 'list_created_at';

/**
 * A page cursor, as one opaque string.
 *
 * The list merges one query per engine, so resuming needs each engine's own
 * position. Handing the caller a single token keeps that shape out of the client:
 * a cursor is something to pass back, not something to understand.
 */
export interface EnginePosition {
  engine: string;
  lastKey: Record<string, unknown>;
}

export function encodeCursor(positions: EnginePosition[]): string {
  if (!positions.length) return '';
  return Buffer.from(JSON.stringify(positions), 'utf8').toString('base64url');
}

/**
 * Reads a cursor back, refusing anything malformed.
 *
 * A cursor arrives from the query string and is handed straight to DynamoDB as an
 * exclusive start key, so it is validated rather than trusted: each entry must
 * name an allowed engine and carry a plain object of key attributes. A bad cursor
 * yields no positions, which restarts from the newest page — the wrong page is
 * recoverable, a rejected request in the middle of scrolling is not.
 */
export function decodeCursor(raw: unknown): EnginePosition[] {
  if (typeof raw !== 'string' || !raw) return [];
  try {
    const parsed: unknown = JSON.parse(
      Buffer.from(raw, 'base64url').toString('utf8'),
    );
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((entry): entry is EnginePosition => {
      if (entry === null || typeof entry !== 'object') return false;
      const candidate = entry as Record<string, unknown>;
      const lastKey = candidate.lastKey;
      return (
        typeof candidate.engine === 'string' &&
        isAllowedEngine(candidate.engine) &&
        lastKey !== null &&
        typeof lastKey === 'object' &&
        !Array.isArray(lastKey)
      );
    });
  } catch {
    return [];
  }
}
