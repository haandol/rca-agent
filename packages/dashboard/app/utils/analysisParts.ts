/** A terminal parent can fence a writer before its last RUNNING/WAITING part record changes. */
export function analysisPartInterrupted(
  parentState: string,
  partStatus: string,
): boolean {
  return (
    ['FAILED', 'CANCELLED', 'OUTDATED'].includes(parentState) &&
    ['RUNNING', 'WAITING'].includes(partStatus)
  );
}

/** Stop successful-workflow polling only after all three actual part records are terminal. */
export function analysisPartsComplete(
  parentState: string,
  parts: { part: string; status: string }[],
): boolean {
  return (
    ['COMPLETED', 'FAILED', 'CANCELLED', 'OUTDATED'].includes(parentState) &&
    ['recovery', 'root_cause', 'operations'].every((name) =>
      parts.some(
        (part) =>
          part.part === name &&
          ['COMPLETED', 'FAILED', 'SKIPPED'].includes(part.status),
      ),
    )
  );
}
