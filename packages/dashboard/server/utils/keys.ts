/**
 * The DynamoDB key layout the dashboard reads, in one place.
 *
 * Every session, span, hypothesis, execution and revision for one RCA shares a
 * partition, and which record is which is decided entirely by the sort key. Each
 * handler used to assemble those strings itself, which made two things easy to
 * get wrong: the `RCA#` prefix (a typo there reads an empty partition rather than
 * failing) and the pre engine-split layout, where Strands wrote bare `SESSION` /
 * `SPAN#` / `HYPO#` keys before the engine prefix existed. That fallback has to be
 * applied wherever a Strands record is read, and a handler that forgets it simply
 * reports the session as missing.
 *
 * The engines own this layout — these helpers only name what they already write,
 * so a change here is a read-side follow-on to an engine change, never the
 * decision itself.
 */

/** The engines whose records this dashboard is willing to act on. */
export const ALLOWED_ENGINES = [
  'strands',
  'codex-headless',
  'cc-headless',
] as const;

export type Engine = (typeof ALLOWED_ENGINES)[number];

export function isAllowedEngine(engine: string): engine is Engine {
  return (ALLOWED_ENGINES as readonly string[]).includes(engine);
}

export function rcaPk(rcaId: string): string {
  return `RCA#${rcaId}`;
}

export function rcaIdFromPk(pk: string): string {
  return pk.replace('RCA#', '');
}

export function sessionSk(engine: string): string {
  return `${engine}#SESSION`;
}

export function executionSk(executionId: string): string {
  return `EXEC#${executionId}`;
}

export function playbookRevisionSk(engine: string): string {
  return `${engine}#PLAYBOOK_REVISION`;
}

export const EXECUTION_SK_PREFIX = 'EXEC#';
export const ACTIVE_EXECUTION_SK = 'EXEC_ACTIVE';

/**
 * The session sort keys to try for an engine, newest layout first.
 *
 * Strands predates the engine prefix, so its session may live under either key
 * and both have to be tried before concluding the session does not exist.
 */
export function sessionSkCandidates(engine: string): string[] {
  return engine === 'strands'
    ? [sessionSk(engine), 'SESSION']
    : [sessionSk(engine)];
}

/** Sort keys that hold a session record, across engines and legacy layouts. */
export function isSessionSortKey(sortKey: string): boolean {
  return sortKey === 'SESSION' || sortKey.endsWith('#SESSION');
}

/**
 * Derive the engine that owns an item from its sort key.
 *
 * Pre engine-split records use bare `SESSION` / `SPAN#` / `HYPO#` keys and are
 * always Strands; newer records prefix the sort key with the engine name.
 */
export function parseEngine(sk: string): string {
  if (sk === 'SESSION' || sk.startsWith('SPAN#') || sk.startsWith('HYPO#'))
    return 'strands';
  return sk.split('#')[0] ?? 'strands';
}

/** The hypothesis sort-key prefix for a session, honouring the legacy layout. */
export function hypothesisSkPrefix(engine: string, sessionKey: string): string {
  return sessionKey === 'SESSION' ? 'HYPO#' : `${engine}#HYPO#`;
}
