export type PlaybookDocument = Record<string, unknown>;

export interface LibraryItem {
  playbook_id: string;
  revision: string;
  source_rca_id: string;
  engine: string;
  failure_type: string;
  symptom_pattern: string;
  tags: string[];
  verification_status: string;
  updated_at: string | null;
  publication_status: 'PENDING' | 'PUBLISHED';
  availability: 'AVAILABLE' | 'UNAVAILABLE';
  unavailable_reason: string | null;
}

export interface LibraryDetail {
  item: LibraryItem;
  playbook: PlaybookDocument | null;
}

export interface LibraryPage {
  items: LibraryItem[];
  nextCursor: string | null;
}

export interface PlaybookCandidate {
  playbook_id: string;
  rca_id: string;
  engine: string;
  similarity: number;
  revision: string;
  availability: 'AVAILABLE' | 'UNAVAILABLE';
  applicable: boolean | null;
  rationale: string;
}

export interface PlaybookProposal {
  proposal_id: string;
  playbook_id: string;
  base_revision: string;
  source_rca_id: string;
  source_engine: string;
  before: PlaybookDocument;
  after: PlaybookDocument;
  changes: { field: string; before: unknown; after: unknown }[];
  rationale: string;
  evidence: string[];
  state: 'PENDING' | 'APPLIED' | 'REJECTED';
  result_revision?: string;
  publication_status?: 'PENDING' | 'PUBLISHED';
  publication_error?: string;
}
export interface PlaybookUsedReference {
  role: string;
  ref: string;
  playbook_id?: string;
  revision?: string;
  source_rca_id?: string;
  source_engine?: string;
  source_path?: string;
}

export interface PlaybookComparison {
  status:
    | 'UPDATE_PROPOSED'
    | 'NO_CHANGE'
    | 'NO_MATCH'
    | 'NO_APPLICABLE_MATCH'
    | 'SEARCH_FAILED';
  query: string;
  selected_playbook_id: string | null;
  baseline?: {
    playbook_id: string;
    revision: string;
    source_rca_id: string;
    source_engine: string;
    playbook: PlaybookDocument;
  } | null;
  evidence?: { ref: string; value: unknown }[];
  source_roles?: unknown;
  used_references?: PlaybookUsedReference[];
  inputs?: { current_report?: unknown; analysis?: unknown };
  candidates: PlaybookCandidate[];
  proposal?: PlaybookProposal;
}

export interface ProposalResponse {
  rca_id: string;
  engine: string;
  comparison: PlaybookComparison | null;
  unavailable_reason: string | null;
  summary?: {
    status: PlaybookComparison['status'];
    selected_playbook_id: string | null;
    proposal?: Pick<
      PlaybookProposal,
      | 'proposal_id'
      | 'state'
      | 'result_revision'
      | 'publication_status'
      | 'publication_error'
    >;
  } | null;
}
