export interface DocFile {
  name: string;
  size: number;
  /** Base64 payload; empty for a history placeholder whose bytes could not be restored. */
  data: string;
  bytes: Uint8Array<ArrayBuffer>;
  mimeType: string;
}

export type DocSide = 'old' | 'new';

export interface DiffItem {
  section?: string;
  page?: string;
  type?: string;
  criticality?: string;
  before?: string;
  after?: string;
  rationale?: string;
}

export interface ImagePair {
  status: 'modified' | 'added' | 'removed' | 'orientation_changed';
  old_page: number | null;
  new_page: number | null;
  old_b64: string | null;
  new_b64: string | null;
  index: number;
}

export interface TokenUsage {
  input_tokens?: number;
  output_tokens?: number;
  thinking_tokens?: number;
  total_tokens?: number;
  cost_eur?: number;
}

export interface FeedbackProps {
  submissionKey: string;
  messageId: number | null;
}

export interface DocSummaryMeta {
  truncated: boolean;
  durationS: number;
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  costEur: number;
}

export interface DocSummaryState {
  text: string;
  error: string;
  loading: boolean;
  noContent: boolean;
  meta: DocSummaryMeta | null;
}

export const EMPTY_SUMMARY: DocSummaryState = { text: '', error: '', loading: false, noContent: false, meta: null };
