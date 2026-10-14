export interface Source {
  title: string;
  url?: string;
  /** 1-based inline-citation number; set only for sources cited inline (⟦n⟧). */
  n?: number;
  // From the document catalog at answer time: document title, current revision and its date (ISO).
  doc_title?: string;
  revision?: string;
  doc_date?: string;
}

export interface Feedback {
  vote: 'up' | 'down';
  comment?: string | null;
}

export interface Message {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  streaming?: boolean;
  status?: string;
  dbId?: number;
  sessionId?: string;
  error?: boolean;
  sources?: Source[];
  /** Only populated on the shared read-only view: the owner's vote, never editable by a viewer. */
  feedback?: Feedback | null;
}

/** "Version B (dd/mm/yyyy)", "Version B", "dd/mm/yyyy" or '' — the same wording the answer model uses. */
export function versionLabel(src: Source): string {
  const date = src.doc_date ? new Date(src.doc_date).toLocaleDateString('en-GB') : '';
  if (src.revision) return date ? `Version ${src.revision} (${date})` : `Version ${src.revision}`;
  return date;
}
