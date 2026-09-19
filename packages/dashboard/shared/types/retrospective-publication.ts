/** Server-attested follow-up metadata; original execution/retrospective fields remain independent. */
export interface PublicationAttempt {
  attemptId: string;
  reviewStatus: 'COMPLETED';
  status:
    | 'WAITING_FOR_PUBLICATION'
    | 'PUBLISHING'
    | 'PUBLISHED'
    | 'BLOCKED'
    | 'FAILED';
  reason: string;
  createdAt: number;
  updatedAt: number;
  expiresAt: number;
  publicPlaybookId?: string;
  publishedRevision?: string;
}

export interface PublicationHistory {
  publicationAttempts?: PublicationAttempt[];
  publicationReadState?: 'AVAILABLE' | 'UNAVAILABLE' | 'NOT_RECORDED';
  retrospectiveStatus?: string;
  sourcePart?: string;
  state?: string;
}
