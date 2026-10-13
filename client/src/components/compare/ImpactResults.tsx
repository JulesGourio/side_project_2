import { createContext, useContext, useMemo, useState } from 'react';
import { CheckCircle2, Database, Download, ExternalLink, Loader2, Send, ThumbsDown, ThumbsUp } from 'lucide-react';
import { toast } from 'sonner';

// ---------------------------------------------------------------------------
// Impact search result — streamed from POST /api/compare/impact (NDJSON):
// plan → one document per judged candidate → done.
// ---------------------------------------------------------------------------

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
  status?: 'impacted' | 'check' | 'not_impacted' | 'error';
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

const STATUS_ORDER: Record<string, number> = { impacted: 0, check: 1, not_impacted: 2, error: 3 };

const STATUS_STYLE: Record<string, { label: string; color: string; border: string }> = {
  impacted: { label: 'Impacted', color: '#16a34a', border: '#16a34a' },
  check: { label: 'To check', color: '#d97706', border: '#d97706' },
  not_impacted: { label: 'Not impacted', color: 'var(--color-text-muted)', border: 'transparent' },
  error: { label: 'Judgment failed', color: 'var(--color-error)', border: 'var(--color-error)' },
};

const CRIT_COLOR: Record<string, string> = { high: '#dc2626', medium: '#d97706', low: '#6b7280' };
const ARCHIVE_COLOR = '#a16207';

export function sortImpactDocs(docs: ImpactDoc[]): ImpactDoc[] {
  return [...docs].sort((a, b) =>
    (STATUS_ORDER[a.status ?? ''] ?? 9) - (STATUS_ORDER[b.status ?? ''] ?? 9)
    || b.change_ids.length - a.change_ids.length,
  );
}

function Chip({ children, color, title }: { children: React.ReactNode; color: string; title?: string }) {
  return (
    <span
      title={title}
      className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-semibold whitespace-nowrap"
      style={{ color, background: `color-mix(in srgb, ${color} 12%, transparent)` }}
    >
      {children}
    </span>
  );
}

// Fired when a change id is clicked; the Change Table listens and scrolls to that row.
export const FOCUS_CHANGE_EVENT = 'qualibot:focus-change';

// True when the change ids of this result are the row numbers of the Change Table on screen.
const ChangeLinkContext = createContext(false);

// What a vote is attached to, and the votes already cast in this browser.
interface FeedbackTarget {
  impactRequestId: number | null;
  messageId: number | null;
  oldFileHash: string;
  newFileHash: string;
}
const FeedbackContext = createContext<FeedbackTarget | null>(null);

const LS_IMPACT_VOTES = 'compare_impact_votes'; // { "<request id>:<ref>": "up" | "down" }

function readVotes(): Record<string, string> {
  try { return JSON.parse(localStorage.getItem(LS_IMPACT_VOTES) || '{}'); } catch { return {}; }
}

function rememberVote(key: string, vote: string): void {
  try {
    const votes = readVotes();
    votes[key] = vote;
    const keys = Object.keys(votes);
    for (const k of keys.slice(0, Math.max(0, keys.length - 300))) delete votes[k];
    localStorage.setItem(LS_IMPACT_VOTES, JSON.stringify(votes));
  } catch { /* storage unavailable — the vote is still sent */ }
}

async function sendImpactFeedback(target: FeedbackTarget, body: Record<string, unknown>): Promise<void> {
  const res = await fetch('/api/compare/impact/feedback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      impact_request_id: target.impactRequestId,
      message_id: target.messageId,
      old_file_hash: target.oldFileHash || null,
      new_file_hash: target.newFileHash || null,
      ...body,
    }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
}

function ChangeChip({ id, changes }: { id: string; changes: Record<string, ImpactChange> }) {
  const c = changes[id];
  const linked = useContext(ChangeLinkContext);
  const className = 'inline-flex px-1.5 py-0.5 rounded text-[10px] font-mono font-medium';
  const style = { color: 'var(--color-accent-primary)', background: 'color-mix(in srgb, var(--color-accent-primary) 12%, transparent)' };
  if (!linked) return <span title={c ? c.text : id} className={className} style={style}>{id}</span>;
  return (
    <button
      type="button"
      title="Show this change in the Change Table"
      onClick={() => window.dispatchEvent(new CustomEvent(FOCUS_CHANGE_EVENT, { detail: id }))}
      className={`${className} cursor-pointer underline decoration-dotted underline-offset-2`}
      style={style}
    >
      {id}
    </button>
  );
}

// "Is this verdict right?" — one click, no comment: the per-document signal used to measure the judge.
function VerdictVote({ doc }: { doc: ImpactDoc }) {
  const target = useContext(FeedbackContext);
  const key = `${target?.impactRequestId ?? 'none'}:${doc.ref}`;
  const [vote, setVote] = useState<string | null>(() => readVotes()[key] ?? null);
  if (!target || !doc.status || doc.status === 'error') return null;

  const cast = async (v: 'up' | 'down') => {
    if (vote === v) return;
    setVote(v);
    rememberVote(key, v);
    try {
      await sendImpactFeedback(target, { vote: v, ref: doc.ref, verdict_shown: doc.status });
    } catch {
      toast.error('Your vote could not be saved.');
    }
  };
  const btn = (v: 'up' | 'down', label: string, color: string, Icon: typeof ThumbsUp) => (
    <button
      type="button"
      onClick={() => cast(v)}
      title={label}
      aria-label={label}
      aria-pressed={vote === v}
      className="p-1 rounded cursor-pointer transition-colors"
      style={{ color: vote === v ? color : 'var(--color-text-muted)', background: vote === v ? `${color}18` : 'transparent' }}
    >
      <Icon className="h-3.5 w-3.5" />
    </button>
  );
  return (
    <span className="ml-auto flex items-center gap-0.5">
      {btn('up', 'This verdict is correct', '#16a34a', ThumbsUp)}
      {btn('down', 'This verdict is wrong', '#dc2626', ThumbsDown)}
    </span>
  );
}

// Same pattern as the analysis cards: a vote on the whole result, with an optional comment.
function SearchFeedback() {
  const target = useContext(FeedbackContext);
  const key = `${target?.impactRequestId ?? 'none'}:`;
  const [vote, setVote] = useState<'up' | 'down' | null>(null);
  const [comment, setComment] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [done, setDone] = useState(() => key in readVotes());
  if (!target) return null;

  if (done) {
    return (
      <div className="flex items-center gap-2 text-sm" style={{ color: 'var(--color-success)' }}>
        <CheckCircle2 className="h-4 w-4" />
        <span className="font-medium">Thank you for your feedback!</span>
      </div>
    );
  }
  const submit = async () => {
    if (!vote || submitting) return;
    setSubmitting(true);
    try {
      await sendImpactFeedback(target, { vote, comment: comment.trim() || null });
      rememberVote(key, vote);
      setDone(true);
    } catch {
      toast.error('Your feedback could not be saved.');
    } finally {
      setSubmitting(false);
    }
  };
  const btn = (v: 'up' | 'down', label: string, color: string, Icon: typeof ThumbsUp) => (
    <button
      type="button"
      onClick={() => setVote(v)}
      className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium border cursor-pointer transition-colors"
      style={{
        borderColor: vote === v ? color : 'var(--color-border)',
        color: vote === v ? color : 'var(--color-text-muted)',
        background: vote === v ? `${color}10` : 'transparent',
      }}
    >
      <Icon className="h-3.5 w-3.5" />
      {label}
    </button>
  );
  return (
    <div className="pt-3 border-t border-[var(--color-border)]/40 space-y-2.5">
      <div className="flex flex-wrap items-center gap-2.5">
        <span className="text-xs font-medium text-[var(--color-text-heading)]">Was this impact search helpful?</span>
        {btn('up', 'Helpful', '#16a34a', ThumbsUp)}
        {btn('down', 'Not helpful', '#dc2626', ThumbsDown)}
      </div>
      {vote && (
        <div className="flex flex-wrap items-end gap-2">
          <textarea
            value={comment}
            onChange={e => setComment(e.target.value)}
            placeholder="Add a comment (optional) — a missing document, a wrong verdict…"
            rows={2}
            maxLength={1000}
            className="flex-1 min-w-[240px] px-3 py-2 rounded-lg border text-xs resize-none outline-none"
            style={{ borderColor: 'var(--color-border)', background: 'var(--color-background)', color: 'var(--color-text-primary)' }}
          />
          <button
            type="button"
            onClick={submit}
            disabled={submitting}
            className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-semibold text-white cursor-pointer disabled:opacity-50"
            style={{ background: 'var(--color-accent-primary)' }}
          >
            <Send className="h-3.5 w-3.5" />
            {submitting ? 'Sending…' : 'Submit'}
          </button>
        </div>
      )}
    </div>
  );
}

// The change a passage conflicts with, spelled out: a bare "C37" means nothing
// to a reader when the comparison has dozens of changes.
function ChangeLine({ id, changes }: { id: string; changes: Record<string, ImpactChange> }) {
  const c = changes[id];
  if (!c) return <ChangeChip id={id} changes={changes} />;
  return (
    <div className="flex flex-wrap items-baseline gap-x-1.5 text-xs break-words">
      <ChangeChip id={id} changes={changes} />
      {c.section && <span className="font-semibold" style={{ color: 'var(--color-text-heading)' }}>{c.section}</span>}
      {c.before || c.after ? (
        <span>
          {c.before && <><del style={{ color: 'var(--color-error)' }}>{c.before}</del> → </>}
          <span className="font-semibold" style={{ color: '#16a34a' }}>{c.after}</span>
        </span>
      ) : (
        <span className="line-clamp-2">{c.summary || c.text}</span>
      )}
    </div>
  );
}

function DocLink({ doc }: { doc: ImpactDoc }) {
  const label = doc.ref || String(doc.iddoc);
  return doc.url ? (
    <a href={doc.url} target="_blank" rel="noreferrer" className="text-sm font-semibold hover:underline" style={{ color: 'var(--color-accent-primary)' }}>
      {label} <ExternalLink className="inline h-3 w-3 ml-0.5" />
    </a>
  ) : (
    <span className="text-sm font-semibold">{label}</span>
  );
}

function ArchiveChip({ doc }: { doc: ImpactDoc }) {
  if (!doc.archive) return null;
  return (
    <Chip color={ARCHIVE_COLOR} title="Published before 2018">
      Archive {doc.doc_date.slice(0, 4)}
    </Chip>
  );
}

function HighlightedText({ text, highlight }: { text: string; highlight: [number, number] | null }) {
  if (!highlight) return <>{text}</>;
  const [s, e] = highlight;
  return (
    <>
      {text.slice(0, s)}
      <mark className="rounded px-0.5" style={{ background: '#fde68a', color: '#3b2a00' }}>{text.slice(s, e)}</mark>
      {text.slice(e)}
    </>
  );
}

function PassageView({ p, changes }: { p: ImpactPassage; changes: Record<string, ImpactChange> }) {
  return (
    <div className="rounded-lg p-3 space-y-1.5" style={{ background: 'var(--color-bg-secondary)' }}>
      {(p.section || p.page) && (
        <div className="flex flex-wrap items-center gap-1.5 text-xs">
          {p.section && <span className="font-mono text-[11px]" style={{ color: 'var(--color-text-heading)' }}>{p.section}</span>}
          {p.page && <span style={{ color: 'var(--color-text-muted)' }}>p. {p.page}</span>}
        </div>
      )}
      <p className="text-[13px] whitespace-pre-wrap max-h-48 overflow-y-auto break-words">
        <HighlightedText text={p.text} highlight={p.highlight} />
      </p>
      {p.changes.length > 0 && (
        <div className="space-y-0.5 pt-1.5 border-t border-[var(--color-border)]/50">
          <span className="text-[10px] uppercase font-semibold tracking-wide" style={{ color: 'var(--color-text-muted)' }}>
            Conflicts with
          </span>
          {p.changes.map(id => <ChangeLine key={id} id={id} changes={changes} />)}
        </div>
      )}
      {p.explanation && <p className="text-xs" style={{ color: 'var(--color-text-muted)' }}>→ {p.explanation}</p>}
    </div>
  );
}

function DocCard({ doc, changes }: { doc: ImpactDoc; changes: Record<string, ImpactChange> }) {
  const st = STATUS_STYLE[doc.status ?? ''] ?? STATUS_STYLE.not_impacted;
  const passages = doc.passages ?? [];
  const [open, setOpen] = useState(false);
  return (
    <div className="p-3 rounded-xl border border-[var(--color-border)]/40 space-y-1.5" style={{ borderLeft: `3px solid ${st.border}` }}>
      <div className="flex items-center gap-2 flex-wrap">
        {doc.flag && <span>{doc.flag}</span>}
        <DocLink doc={doc} />
        {doc.title && <span className="text-xs text-[var(--color-text-muted)]">— {doc.title}</span>}
        {doc.division && <Chip color="var(--color-accent-primary)">{doc.division.toUpperCase()}</Chip>}
        <ArchiveChip doc={doc} />
        <Chip color={st.color}>{st.label}</Chip>
        {doc.confidence && <span className="text-xs text-[var(--color-text-muted)]">{doc.confidence} confidence</span>}
        <VerdictVote doc={doc} />
      </div>
      {doc.reason && (
        <p className="text-xs" style={{ color: doc.status === 'error' ? 'var(--color-error)' : 'var(--color-text-primary)' }}>{doc.reason}</p>
      )}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-[var(--color-text-muted)]">
        {passages.length > 0 && <span>{passages.length} passage{passages.length > 1 ? 's' : ''} to update</span>}
        {doc.other_languages.length > 0 && (
          <span className="flex items-center gap-1 flex-wrap">
            also in:
            {doc.other_languages.map(o => (
              o.url
                ? <a key={o.ref} href={o.url} target="_blank" rel="noreferrer" className="hover:underline">{o.flag} {o.ref}</a>
                : <span key={o.ref}>{o.flag} {o.ref}</span>
            ))}
          </span>
        )}
      </div>
      {passages.length > 0 && (
        <div>
          <button
            onClick={() => setOpen(v => !v)}
            className="text-xs font-semibold cursor-pointer"
            style={{ color: 'var(--color-accent-primary)' }}
          >
            {open ? '▾ Hide passages' : '▸ Show passages'}
          </button>
          {open && (
            <div className="space-y-2 mt-2">
              {passages.map(p => <PassageView key={p.chunk_id} p={p} changes={changes} />)}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ChangeBlock({ change, docs, changes }: { change: ImpactChange; docs: ImpactDoc[]; changes: Record<string, ImpactChange> }) {
  const hits = docs.flatMap(d => (d.passages ?? []).filter(p => p.changes.includes(change.id)).map(p => ({ d, p })));
  const docCount = new Set(hits.map(h => h.d.ref)).size;
  return (
    <div className="p-3 rounded-xl border border-[var(--color-border)]/40 space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <ChangeChip id={change.id} changes={changes} />
        {change.criticality && <Chip color={CRIT_COLOR[change.criticality] ?? '#6b7280'}>{change.criticality}</Chip>}
        {change.section && <span className="text-sm font-semibold" style={{ color: 'var(--color-text-heading)' }}>{change.section}</span>}
        {change.searched && (
          <span className="text-xs text-[var(--color-text-muted)]">
            {hits.length} passage{hits.length !== 1 ? 's' : ''} · {docCount} doc{docCount !== 1 ? 's' : ''}
          </span>
        )}
      </div>
      <p className="text-[13px] break-words">
        {change.before || change.after ? (
          <>
            {change.before && <><del style={{ color: 'var(--color-error)' }}>{change.before}</del> → </>}
            <span className="font-semibold" style={{ color: '#16a34a' }}>{change.after}</span>
          </>
        ) : change.summary || change.text}
      </p>
      {!change.searched ? (
        <p className="text-xs italic text-[var(--color-text-muted)]">Editorial change — not searched.</p>
      ) : hits.length === 0 ? (
        <p className="text-xs italic text-[var(--color-text-muted)]">No judged document conflicts with this change.</p>
      ) : (
        <div className="space-y-2 pl-3 border-l-2 border-[var(--color-border)]/60">
          {hits.map(({ d, p }) => {
            const st = STATUS_STYLE[d.status ?? ''] ?? STATUS_STYLE.not_impacted;
            return (
              <div key={`${d.ref}-${p.chunk_id}`} className="space-y-0.5">
                <div className="flex flex-wrap items-center gap-1.5">
                  <DocLink doc={d} />
                  <ArchiveChip doc={d} />
                  <Chip color={st.color}>{st.label}</Chip>
                  {p.section && <span className="font-mono text-[11px]" style={{ color: 'var(--color-text-heading)' }}>{p.section}</span>}
                </div>
                {p.quote && <p className="text-[13px] break-words">« {p.quote} »</p>}
                {p.explanation && <p className="text-xs text-[var(--color-text-muted)]">→ {p.explanation}</p>}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// Changes that conflict with at least one document first, in full; the others
// (often the vast majority on a heavily revised document) folded into one line.
function ChangeList({ changeList, docs, changes }: { changeList: ImpactChange[]; docs: ImpactDoc[]; changes: Record<string, ImpactChange> }) {
  const hit = new Set(docs.flatMap(d => (d.passages ?? []).flatMap(p => p.changes)));
  const withHits = changeList.filter(c => hit.has(c.id));
  const without = changeList.filter(c => !hit.has(c.id));
  return (
    <>
      {withHits.length === 0 && (
        <p className="text-sm italic text-[var(--color-text-muted)]">No change conflicts with a document.</p>
      )}
      {withHits.map(c => <ChangeBlock key={c.id} change={c} docs={docs} changes={changes} />)}
      {without.length > 0 && (
        <details className="text-xs">
          <summary className="cursor-pointer font-semibold" style={{ color: 'var(--color-accent-primary)' }}>
            {without.length} other change{without.length > 1 ? 's' : ''} with no document to update
          </summary>
          <div className="space-y-1 mt-2">
            {without.map(c => <ChangeLine key={c.id} id={c.id} changes={changes} />)}
          </div>
        </details>
      )}
    </>
  );
}

function SegButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      onClick={onClick}
      className="px-2.5 py-1 rounded-full border text-xs font-semibold cursor-pointer transition-colors"
      style={active
        ? { borderColor: 'var(--color-accent-primary)', color: 'var(--color-accent-primary)', background: 'color-mix(in srgb, var(--color-accent-primary) 10%, transparent)' }
        : { borderColor: 'var(--color-border)', color: 'var(--color-text-muted)', background: 'transparent' }}
    >
      {children}
    </button>
  );
}

type StatusFilter = 'all' | 'impacted' | 'check' | 'not_impacted';

export function ImpactResultsCard({
  result,
  isLoading,
  error,
  accentColor,
  exportName,
  changeTableShown,
  messageId,
  oldFileHash,
  newFileHash,
}: {
  result: ImpactResult | null;
  isLoading: boolean;
  error: string;
  accentColor: string;
  exportName: string;
  // The Change Table this result was computed from is on screen: change ids link to its rows.
  changeTableShown: boolean;
  messageId: number | null;
  oldFileHash: string;
  newFileHash: string;
}) {
  const [view, setView] = useState<'doc' | 'change'>('doc');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
  const [exporting, setExporting] = useState(false);

  const changes = useMemo(
    () => Object.fromEntries((result?.changes ?? []).map(c => [c.id, c])),
    [result?.changes],
  );
  const docs = useMemo(() => sortImpactDocs(result?.documents ?? []), [result?.documents]);
  const visible = docs.filter(d => statusFilter === 'all' || d.status === statusFilter);
  const count = (s: string) => docs.filter(d => d.status === s).length;
  const passageCount = docs.reduce((n, d) => n + (d.passages?.length ?? 0), 0);
  const notSearched = (result?.changes ?? []).filter(c => !c.searched).map(c => c.id);
  const feedbackTarget = useMemo<FeedbackTarget | null>(
    () => (result?.done && !result.no_changes
      ? { impactRequestId: result.impact_request_id ?? null, messageId, oldFileHash, newFileHash }
      : null),
    [result?.done, result?.no_changes, result?.impact_request_id, messageId, oldFileHash, newFileHash],
  );

  const handleExport = async () => {
    if (!result) return;
    setExporting(true);
    try {
      const form = new FormData();
      form.append('result_json', JSON.stringify({ ...result, documents: docs }));
      form.append('filename', exportName);
      const res = await fetch('/api/compare/impact/export-excel', { method: 'POST', body: form });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
        throw new Error(err.error ?? res.statusText);
      }
      const url = URL.createObjectURL(await res.blob());
      const a = document.createElement('a');
      a.href = url;
      a.download = `${exportName}.xlsx`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      toast.error(`Excel export failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setExporting(false);
    }
  };

  return (
    <ChangeLinkContext.Provider value={changeTableShown && result?.source === 'structured'}>
    <FeedbackContext.Provider value={feedbackTarget}>
    <div className="rounded-2xl border border-[var(--color-border)]/40 bg-[var(--color-background)] shadow-sm overflow-hidden">
      <div className="h-0.5 w-full" style={{ background: accentColor }} />

      <div className="flex flex-wrap items-center justify-between gap-2 px-5 py-4 border-b border-[var(--color-border)]/30 bg-[var(--color-bg-secondary)]/40">
        <div className="flex items-center gap-2.5">
          <div className="w-7 h-7 rounded-lg flex items-center justify-center" style={{ background: `${accentColor}18` }}>
            <Database className="h-3.5 w-3.5" style={{ color: accentColor }} />
          </div>
          <span className="text-sm font-semibold text-[var(--color-text-heading)]">Impacted Docs</span>
          {isLoading && (
            <span className="flex items-center gap-1.5 text-xs text-[var(--color-text-muted)]">
              <Loader2 className="h-3 w-3 animate-spin" style={{ color: accentColor }} />
              {result ? `Reviewing documents… ${docs.length}/${result.candidates}` : 'Searching the knowledge base…'}
            </span>
          )}
        </div>
      </div>

      <div className="p-5 space-y-4 text-sm">
        {error ? (
          <p className="text-sm" style={{ color: 'var(--color-error)' }}>{error}</p>
        ) : !result ? (
          <div className="flex items-center gap-2 text-[var(--color-text-muted)]">
            <Loader2 className="h-4 w-4 animate-spin" style={{ color: accentColor }} />
            <span className="italic text-sm">Searching the knowledge base…</span>
          </div>
        ) : result.no_changes ? (
          <p className="text-sm italic text-[var(--color-text-muted)]">The analysis reported no substantive change — nothing to search.</p>
        ) : (
          <>
            <div className="flex flex-wrap items-baseline gap-x-6 gap-y-1">
              {[
                [count('impacted'), 'impacted', '#16a34a'],
                [count('check'), 'to check', '#d97706'],
                [count('not_impacted'), 'not impacted', 'var(--color-text-muted)'],
                [passageCount, 'passages to update', 'var(--color-text-heading)'],
              ].map(([n, label, color]) => (
                <span key={label as string} className="text-xs text-[var(--color-text-muted)]">
                  <b className="text-lg mr-1 tabular-nums" style={{ color: color as string }}>{n}</b>{label}
                </span>
              ))}
            </div>
            {(result.excluded_refs.length > 0 || notSearched.length > 0) && (
              <p className="text-xs text-[var(--color-text-muted)]">
                {result.excluded_refs.length > 0 && <>Compared document left out: {result.excluded_refs.join(', ')}. </>}
                {notSearched.length > 0 && <>Editorial change{notSearched.length > 1 ? 's' : ''} ({notSearched.join(', ')}) not checked.</>}
              </p>
            )}

            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex flex-wrap gap-1.5">
                <SegButton active={view === 'doc'} onClick={() => setView('doc')}>By document</SegButton>
                <SegButton active={view === 'change'} onClick={() => setView('change')}>By change</SegButton>
              </div>
              <button
                onClick={handleExport}
                disabled={!result.done || exporting}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium border cursor-pointer disabled:opacity-40"
                style={{ color: accentColor, borderColor: 'var(--color-border)' }}
                title="One row per passage to update, plus the documents and the change list"
              >
                {exporting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
                Export Excel
              </button>
            </div>

            <div className="flex flex-wrap items-center gap-1.5">
              {([['all', 'All'], ['impacted', 'Impacted'], ['check', 'To check'], ['not_impacted', 'Not impacted']] as [StatusFilter, string][]).map(([k, label]) => (
                <SegButton key={k} active={statusFilter === k} onClick={() => setStatusFilter(k)}>{label}</SegButton>
              ))}
            </div>

            <div className="max-h-[900px] overflow-y-auto space-y-2.5 pr-1">
              {view === 'doc' ? (
                visible.length === 0 ? (
                  <p className="text-sm italic text-[var(--color-text-muted)]">
                    {isLoading ? 'Waiting for the first judgments…' : 'No document matches these filters.'}
                  </p>
                ) : visible.map(d => <DocCard key={`${d.iddoc}-${d.ref}`} doc={d} changes={changes} />)
              ) : (
                <ChangeList changeList={result.changes} docs={visible} changes={changes} />
              )}
            </div>

            {/* key: a new search starts with a blank feedback form */}
            <SearchFeedback key={result.impact_request_id ?? 'none'} />
          </>
        )}
      </div>
    </div>
    </FeedbackContext.Provider>
    </ChangeLinkContext.Provider>
  );
}
