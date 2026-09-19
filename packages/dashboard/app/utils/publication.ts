import type { PublicationHistory } from '../../shared/types/retrospective-publication';

/** Failed publishing is retryable by the idle worker; blocked/published records are terminal views. */
export function shouldPollPublication(
  value: PublicationHistory | null | undefined,
): boolean {
  if (!value || value.state !== 'RESOLVED') return false;
  const attempts = value.publicationAttempts ?? [];
  if (value.publicationReadState === 'UNAVAILABLE') return false;
  if (attempts.length) {
    const latest = attempts[0]!;
    return (
      latest.expiresAt > Date.now() / 1000 &&
      ['WAITING_FOR_PUBLICATION', 'PUBLISHING', 'FAILED'].includes(
        latest.status,
      )
    );
  }
  return (
    value.retrospectiveStatus === 'RUNNING' ||
    (value.sourcePart === 'recovery' &&
      value.retrospectiveStatus === 'COMPLETED')
  );
}

/** Keep wire values stable while explaining publication separately from incident resolution. */
export const PUBLICATION_LABELS = {
  WAITING_FOR_PUBLICATION: '공용 반영 대기',
  PUBLISHING: '공용 반영 중',
  PUBLISHED: '공용 반영 완료',
  BLOCKED: '공용 반영 차단',
  FAILED: '공용 반영 실패',
} as const;
