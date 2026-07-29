/**
 * Derive the engine that owns a DynamoDB item from its sort key.
 *
 * Pre engine-split records use bare `SESSION` / `SPAN#` / `HYPO#` keys and are
 * always Strands; newer records prefix the sort key with the engine name.
 */
export function parseEngine(sk: string): string {
  if (sk === 'SESSION' || sk.startsWith('SPAN#') || sk.startsWith('HYPO#'))
    return 'strands';
  return sk.split('#')[0] ?? 'strands';
}
