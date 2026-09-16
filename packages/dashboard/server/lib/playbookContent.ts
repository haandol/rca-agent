type Row = Record<string, any>;

const AUXILIARY = new Set([
  'comparison',
  'library_revision',
  'source_engine',
  'source_rca_id',
  'stage',
  'summary',
  'output_summary',
]);

/**
 * Compare the complete historical domain without lookup or presentation fields.
 * Unknown domain fields and the entire runbook remain part of the baseline.
 */
export function domainPlaybook(value: Row): Row {
  return Object.fromEntries(
    Object.entries(value).filter(([key]) => !AUXILIARY.has(key)),
  );
}
/**
 * Check publication identity without request-specific source annotations.
 * Unlike a knowledge baseline, the published payload retains its comparison.
 */
export function publishedPayload(value: Row): Row {
  return Object.fromEntries(
    Object.entries(value).filter(
      ([key]) =>
        !['library_revision', 'source_engine', 'source_rca_id'].includes(key),
    ),
  );
}
/**
 * Keep dashboard publication in the Python writers' embedding space.
 * Match their label order, 80-code-point truncation and omission of empty fields.
 */
export function libraryEmbedKey(book: Row, metricName: string): string {
  return [
    ['장애유형', book.failure_type],
    ['증상', book.symptom_pattern],
    ['메트릭', metricName],
  ]
    .flatMap(([label, value]) => {
      const text =
        typeof value === 'string'
          ? [...value].slice(0, 80).join('').trim()
          : '';
      return text ? [`${label}: ${text}`] : [];
    })
    .join(' | ');
}
