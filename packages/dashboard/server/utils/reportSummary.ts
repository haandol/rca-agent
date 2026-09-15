/** Analysis-time presentation data; execution permission is checked separately. */
export interface ReportSummary {
  incidentSummary: string | null;
  impactSummary: string | null;
  severity: string | null;
  rootCause: string | null;
  confirmed: boolean | null;
  confidence: number | null;
  nextAction: string | null;
  runbookVerificationStatus: string | null;
  runbookApprovalEligible: boolean | null;
  playbookId: string | null;
  selectedPlaybookId: string | null;
  comparisonStatus: string | null;
  proposalState: string | null;
}

const SUMMARY_COMMENT = /<!-- rca-summary:v1\s*\n([\s\S]*?)\n-->/g;
const SEVERITIES = new Set(['critical', 'high', 'medium', 'low']);
const COMPARISON_STATES = new Set([
  'UPDATE_PROPOSED',
  'NO_CHANGE',
  'NO_MATCH',
  'NO_APPLICABLE_MATCH',
  'SEARCH_FAILED',
]);

/** Accept only recorded text, leaving absent or malformed values visibly missing. */
function text(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

/** Normalize only a stated probability; percentages are handled by the legacy reader. */
function probability(value: unknown): number | null {
  return typeof value === 'number' &&
    Number.isFinite(value) &&
    value >= 0 &&
    value <= 1
    ? value
    : null;
}

/** Read one known Markdown section without consuming a following section's claims. */
function section(markdown: string, titles: string[]): string {
  const lines = markdown.split(/\r?\n/);
  const index = lines.findIndex(
    (line) =>
      /^##\s+/.test(line) && titles.includes(line.replace(/^##\s+/, '').trim()),
  );
  if (index < 0) return '';
  const remaining = lines.slice(index + 1);
  const end = remaining.findIndex((line) => /^#{1,2}\s/.test(line));
  return remaining
    .slice(0, end < 0 ? undefined : end)
    .join('\n')
    .trim();
}

/** Keep source prose readable without interpreting HTML or inventing a shorter claim. */
function paragraph(source: string): string | null {
  const value = source.split(/\n\s*\n/)[0]?.trim();
  if (!value || /^(?:[-*]\s|\|)/.test(value)) return null;
  return value;
}

/** Extract only an explicitly labeled legacy value, never an inference from prose. */
function label(source: string, names: string[]): string | null {
  for (const line of source.split(/\r?\n/)) {
    const plain = line
      .replace(/^\s*[-*]\s*/, '')
      .replace(/\*\*/g, '')
      .trim();
    for (const name of names) {
      if (plain.toLowerCase().startsWith(`${name.toLowerCase()}:`)) {
        return text(plain.slice(name.length + 1));
      }
    }
  }
  return null;
}

/** Recover the new server marker only when there is exactly one valid object. */
function metadata(markdown: string): Record<string, unknown> | null {
  const matches = [...markdown.matchAll(SUMMARY_COMMENT)];
  if (matches.length !== 1) return null;
  try {
    const parsed: unknown = JSON.parse(matches[0]![1]!);
    return parsed !== null &&
      typeof parsed === 'object' &&
      !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

/**
 * Build an overview without rewriting stored reports.
 *
 * New reports carry a server-owned marker. Older reports expose only explicitly
 * recorded fields; missing values remain null. Session verdicts override report
 * text so an old or malformed document cannot turn an unconfirmed RCA into one
 * that appears confirmed.
 */
export function readReportSummary(
  markdown: string,
  authority: Record<string, unknown> = {},
): ReportSummary {
  const recorded = metadata(markdown);
  const incident = section(markdown, ['Incident Summary', '인시던트 요약']);
  const root = section(markdown, ['Root Cause', '근본 원인']);
  const impact = section(markdown, ['Impact Assessment', '영향', '영향 평가']);
  const mitigation = section(markdown, [
    'Temporary Mitigation',
    '임시 조치',
    '임시 조치 방안',
  ]);
  const confidenceText = label(root, ['Confidence', '신뢰도']);
  const confidenceMatch = confidenceText?.match(
    /^([0-9]+(?:\.[0-9]+)?)\s*(%)?$/,
  );
  const legacyConfidence = confidenceMatch
    ? probability(Number(confidenceMatch[1]) / (confidenceMatch[2] ? 100 : 1))
    : null;
  const severity = text(
    recorded?.severity ?? label(incident, ['Severity', '심각도']),
  )?.toLowerCase();
  const comparison = text(recorded?.comparison_status);
  const verification = text(recorded?.runbook_verification_status);
  const disposition = text(recorded?.proposal_state);
  return {
    incidentSummary: text(recorded?.incident_summary) ?? paragraph(incident),
    impactSummary: text(recorded?.impact_summary) ?? paragraph(impact),
    severity: severity && SEVERITIES.has(severity) ? severity : null,
    rootCause: Object.hasOwn(authority, 'root_cause')
      ? text(authority.root_cause)
      : (text(recorded?.root_cause) ?? paragraph(root)),
    confirmed:
      typeof authority.confirmed === 'boolean'
        ? authority.confirmed
        : typeof recorded?.root_cause_confirmed === 'boolean'
          ? recorded.root_cause_confirmed
          : null,
    confidence:
      probability(authority.confidence_score) ??
      probability(recorded?.confidence_score) ??
      legacyConfidence,
    nextAction: text(recorded?.next_action) ?? paragraph(mitigation),
    runbookVerificationStatus:
      verification && ['DRAFT', 'VERIFIED'].includes(verification)
        ? verification
        : null,
    runbookApprovalEligible:
      typeof recorded?.runbook_approval_eligible === 'boolean'
        ? recorded.runbook_approval_eligible && authority.confirmed !== false
        : null,
    playbookId: text(recorded?.playbook_id),
    selectedPlaybookId: text(recorded?.selected_playbook_id),
    comparisonStatus:
      comparison && COMPARISON_STATES.has(comparison) ? comparison : null,
    proposalState:
      disposition && ['PENDING', 'APPLIED', 'REJECTED'].includes(disposition)
        ? disposition
        : null,
  };
}

/**
 * Hide only the duplicate machine marker from the human rendering.
 *
 * The API also returns the original Markdown unchanged. Invalid markers remain
 * visible as escaped source text rather than silently erasing unknown content.
 */
export function reportDisplayMarkdown(markdown: string): string {
  return metadata(markdown) ? markdown.replace(SUMMARY_COMMENT, '') : markdown;
}
