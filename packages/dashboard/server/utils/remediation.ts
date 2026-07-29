type DataRecord = Record<string, unknown>;

export interface RemediationDetails {
  remediationStatus: string;
  remediationSuccess: boolean | null;
  remediationSummary: string;
  remediationError: string;
  remediationCompletedAt: string;
  verificationStatus: string;
  metricsNormalized: boolean | null;
  verificationSummary: string;
  remainingIssues: string[];
  remediationFaultType: string;
  remediationEndpoint: string;
}

function asRecord(value: unknown): DataRecord | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as DataRecord)
    : null;
}

function readString(...values: unknown[]): string {
  const value = values.find(
    (candidate) => typeof candidate === 'string' && candidate.length > 0,
  );
  return typeof value === 'string' ? value : '';
}

function readBoolean(...values: unknown[]): boolean | null {
  const value = values.find((candidate) => typeof candidate === 'boolean');
  return typeof value === 'boolean' ? value : null;
}

function readMetricsNormalized(
  verificationStatus: string,
  ...values: unknown[]
): boolean | null {
  const explicitValue = readBoolean(...values);
  if (explicitValue !== null) return explicitValue;
  if (verificationStatus === 'NORMALIZED') return true;
  if (verificationStatus === 'FAILED') return false;
  return null;
}

function readStringArray(...values: unknown[]): string[] {
  const value = values.find(
    (candidate) =>
      Array.isArray(candidate) &&
      candidate.every((item) => typeof item === 'string'),
  );
  return Array.isArray(value) ? (value as string[]) : [];
}

function emptyRemediationDetails(): RemediationDetails {
  return {
    remediationStatus: '',
    remediationSuccess: null,
    remediationSummary: '',
    remediationError: '',
    remediationCompletedAt: '',
    verificationStatus: '',
    metricsNormalized: null,
    verificationSummary: '',
    remainingIssues: [],
    remediationFaultType: '',
    remediationEndpoint: '',
  };
}

export function readSessionRemediation(item: DataRecord): RemediationDetails {
  const verificationStatus = readString(item.verification_status);

  return {
    ...emptyRemediationDetails(),
    remediationStatus: readString(item.remediation_status),
    remediationSuccess: readBoolean(item.remediation_success),
    remediationSummary: readString(item.remediation_summary),
    remediationError: readString(item.remediation_error),
    remediationCompletedAt: readString(item.remediation_completed_at),
    verificationStatus,
    metricsNormalized: readMetricsNormalized(
      verificationStatus,
      item.metrics_normalized,
      item.metricsNormalized,
    ),
    verificationSummary: readString(item.verification_summary),
    remainingIssues: readStringArray(
      item.verification_remaining_issues,
      item.remaining_issues,
    ),
    remediationFaultType: readString(
      item.remediation_fault_type,
      item.fault_type,
    ),
    remediationEndpoint: readString(
      item.remediation_endpoint,
      item.endpoint_path,
    ),
  };
}

export function readSpanRemediation(item: DataRecord): RemediationDetails {
  if (item.span_type !== 'REMEDIATION') return emptyRemediationDetails();

  const metadata = asRecord(item.metadata);
  const verification = asRecord(metadata?.verification);
  const verificationStatus = readString(
    verification?.status,
    metadata?.verification_status,
    item.verification_status,
  );

  return {
    remediationStatus: readString(
      metadata?.status,
      metadata?.remediation_status,
      item.remediation_status,
    ),
    remediationSuccess: readBoolean(
      metadata?.success,
      metadata?.remediation_success,
      item.remediation_success,
    ),
    remediationSummary: readString(
      metadata?.summary,
      metadata?.remediation_summary,
      item.remediation_summary,
      item.input_summary,
    ),
    remediationError: readString(
      metadata?.error,
      item.remediation_error,
      item.error,
    ),
    remediationCompletedAt: readString(
      metadata?.completed_at,
      item.remediation_completed_at,
      item.end_time,
    ),
    verificationStatus,
    metricsNormalized: readMetricsNormalized(
      verificationStatus,
      verification?.metrics_normalized,
      metadata?.metrics_normalized,
      item.metrics_normalized,
    ),
    verificationSummary: readString(
      verification?.reason,
      verification?.summary,
      metadata?.verification_summary,
      item.verification_summary,
    ),
    remainingIssues: readStringArray(
      verification?.remaining_issues,
      metadata?.verification_remaining_issues,
      item.verification_remaining_issues,
      metadata?.remaining_issues,
      item.remaining_issues,
    ),
    remediationFaultType: readString(
      metadata?.fault_type,
      metadata?.remediation_fault_type,
      item.remediation_fault_type,
      item.fault_type,
    ),
    remediationEndpoint: readString(
      metadata?.endpoint_path,
      metadata?.remediation_endpoint,
      item.remediation_endpoint,
      item.endpoint_path,
    ),
  };
}

export function mergeRemediationDetails(
  primary: RemediationDetails,
  fallback: RemediationDetails,
): RemediationDetails {
  return {
    remediationStatus: primary.remediationStatus || fallback.remediationStatus,
    remediationSuccess: primary.remediationStatus
      ? primary.remediationSuccess
      : (primary.remediationSuccess ?? fallback.remediationSuccess),
    remediationSummary:
      primary.remediationSummary || fallback.remediationSummary,
    remediationError: primary.remediationError || fallback.remediationError,
    remediationCompletedAt:
      primary.remediationCompletedAt || fallback.remediationCompletedAt,
    verificationStatus:
      primary.verificationStatus || fallback.verificationStatus,
    metricsNormalized: primary.verificationStatus
      ? primary.metricsNormalized
      : (primary.metricsNormalized ?? fallback.metricsNormalized),
    verificationSummary:
      primary.verificationSummary || fallback.verificationSummary,
    remainingIssues:
      primary.verificationStatus || primary.remainingIssues.length
        ? primary.remainingIssues
        : fallback.remainingIssues,
    remediationFaultType:
      primary.remediationFaultType || fallback.remediationFaultType,
    remediationEndpoint:
      primary.remediationEndpoint || fallback.remediationEndpoint,
  };
}
