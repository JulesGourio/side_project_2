// Impact search result — streamed from POST /api/compare/impact (NDJSON):
// plan → one document per judged candidate → done.

export interface ImpactChange {
  id: string;
  section: string;
  type: string;
  criticality: string;
  before: string;
  after: string;
  summary: string;
  text: string;
  searched: boolean;
}

export interface ImpactPassage {
  chunk_id: string;
  section: string;
  page: string;
  text: string;
  quote: string;
  highlight: [number, number] | null;
  changes: string[];
  explanation: string;
}

type ImpactStatus = 'impacted' | 'check' | 'not_impacted' | 'error';

export interface ImpactDoc {
  iddoc: string | number;
  ref: string;
  title: string;
  division: string;
  url: string;
  doc_date: string;
  archive: boolean;
  flag: string;
  change_ids: string[];
  other_languages: { ref: string; url: string; flag: string; source: 'retrieved' | 'catalog' }[];
  judged: boolean;
  status?: ImpactStatus;
  confidence?: string;
  reason?: string;
  sections?: string[];
  passages?: ImpactPassage[];
}

export interface ImpactResult {
  changes: ImpactChange[];
  documents: ImpactDoc[];
  not_judged: ImpactDoc[];
  excluded_refs: string[];
  candidates: number;
  queries_used: number;
  queries_failed: number;
  // Change ids that fit in no search query and were not looked up.
  changes_not_searched?: string[];
  no_changes?: boolean;
  cached?: boolean;
  // 'structured' = change ids match the Change Table rows (C3 = row 3).
  source?: string;
  // impact_requests row, to attach feedback to this search.
  impact_request_id?: number | null;
  done: boolean;
  duration_s?: number;
  usage?: { input_tokens: number; output_tokens: number; total_tokens: number; cost_eur: number };
}

/** Fired when a change id is clicked; the Change Table listens and scrolls to that row. */
export const FOCUS_CHANGE_EVENT = 'qualibot:focus-change';

export type ChangesById = Record<string, ImpactChange>;

const STATUS_ORDER: Record<string, number> = { impacted: 0, check: 1, not_impacted: 2, error: 3 };

const STATUS_STYLE: Record<string, { label: string; color: string; border: string }> = {
  impacted: { label: 'Impacted', color: '#16a34a', border: '#16a34a' },
  check: { label: 'To check', color: '#d97706', border: '#d97706' },
  not_impacted: { label: 'Not impacted', color: 'var(--color-text-muted)', border: 'transparent' },
  error: { label: 'Judgment failed', color: 'var(--color-error)', border: 'var(--color-error)' },
};

export function statusStyle(status: string | undefined) {
  return STATUS_STYLE[status ?? ''] ?? STATUS_STYLE.not_impacted;
}

export function sortImpactDocs(docs: ImpactDoc[]): ImpactDoc[] {
  return [...docs].sort((a, b) =>
    (STATUS_ORDER[a.status ?? ''] ?? 9) - (STATUS_ORDER[b.status ?? ''] ?? 9)
    || b.change_ids.length - a.change_ids.length,
  );
}
