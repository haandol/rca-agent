/**
 * Where a run got to, derived from the spans it recorded.
 *
 * A terminal state overwrites the stage it happened in: a session that died
 * during report generation and one that died on the first metric call both read
 * `FAILED`, and the session record keeps no trace of the difference. The spans
 * do — each one names the stage it covered — so the furthest span is the only
 * evidence of how far a failed run actually got.
 *
 * The mapping below is one-directional and read-only: span types are the
 * engines' vocabulary for work performed, pipeline states are the vocabulary for
 * position, and the two were never the same list. Spans this map does not name
 * (BRANCHING, TERMINATION, NOTIFICATION) are bookkeeping around a stage rather
 * than a stage of their own, so they contribute no position.
 */

/** Span type → the track stage that span means the run had reached. */
const SPAN_TYPE_TO_STATE: Record<string, string> = {
  SCOPING: 'SCOPING',
  HYPOTHESIS_GENERATION: 'HYPOTHESIS_GENERATION',
  PRIORITIZATION: 'HYPOTHESIS_PRIORITIZATION',
  EVIDENCE_COLLECTION: 'EVIDENCE_COLLECTION',
  VALIDATION: 'HYPOTHESIS_VALIDATION',
  VALIDATION_LOOP: 'HYPOTHESIS_VALIDATION',
  REPORT: 'REPORT_GENERATION',
  PLAYBOOK: 'REPORT_GENERATION',
};

/**
 * Stage order per engine, matching the track the dashboard draws.
 *
 * Kept beside the mapping rather than imported from the client module: this is
 * the server deciding which of several spans is furthest, and it must not start
 * depending on render-side code.
 */
const TRACK: Record<string, readonly string[]> = {
  strands: [
    'ALARM_RECEIVED',
    'SCOPING',
    'HYPOTHESIS_GENERATION',
    'HYPOTHESIS_PRIORITIZATION',
    'EVIDENCE_COLLECTION',
    'HYPOTHESIS_VALIDATION',
    'REPORT_GENERATION',
    'COMPLETED',
  ],
  'headless-codex': ['ALARM_RECEIVED', 'ANALYZING', 'COMPLETED'],
  'codex-headless': ['ALARM_RECEIVED', 'ANALYZING', 'COMPLETED'],
  'cc-headless': ['ALARM_RECEIVED', 'ANALYZING', 'COMPLETED'],
};

export function isSpanSortKey(sortKey: string): boolean {
  return sortKey.includes('SPAN#');
}

/**
 * The furthest track stage the given spans reached.
 *
 * Returns '' when no span names a stage on this engine's track — the caller then
 * has no evidence of progress and must not imply any.
 */
export function furthestStage(
  spans: { spanType: string; engine: string }[],
  engine: string,
): string {
  const track = TRACK[engine] ?? TRACK.strands!;
  let best = -1;

  for (const span of spans) {
    // The headless engines record the same span vocabulary but run the whole analysis
    // as one stage, so any work it logged means it had reached ANALYZING.
    if (
      engine === 'headless-codex' ||
      engine === 'codex-headless' ||
      engine === 'cc-headless'
    ) {
      if (SPAN_TYPE_TO_STATE[span.spanType]) {
        best = Math.max(best, track.indexOf('ANALYZING'));
      }
      continue;
    }
    const state = SPAN_TYPE_TO_STATE[span.spanType];
    if (!state) continue;
    best = Math.max(best, track.indexOf(state));
  }

  return best >= 0 ? (track[best] ?? '') : '';
}
