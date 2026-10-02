import { useMemo, useState } from 'react';
import { Database, Download, ExternalLink, Loader2 } from 'lucide-react';
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

function ChangeChip({ id, changes }: { id: string; changes: Record<string, ImpactChange> }) {
  const c = changes[id];
  return (
    <span
      title={c ? c.text : id}
      className="inline-flex px-1.5 py-0.5 rounded text-[10px] font-mono font-medium"
      style={{ color: 'var(--color-accent-primary)', background: 'color-mix(in srgb, var(--color-accent-primary) 12%, transparent)' }}
    >
      {id}
    </span>
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
      <div className="flex flex-wrap items-center gap-1.5 text-xs">
        {p.changes.map(id => <ChangeChip key={id} id={id} changes={changes} />)}
        {p.section && <span className="font-mono text-[11px]" style={{ color: 'var(--color-text-heading)' }}>{p.section}</span>}
        {p.page && <span style={{ color: 'var(--color-text-muted)' }}>p. {p.page}</span>}
      </div>
      <p className="text-[13px] whitespace-pre-wrap max-h-48 overflow-y-auto break-words">
        <HighlightedText text={p.text} highlight={p.highlight} />
      </p>
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
      </div>
      {doc.reason && (
        <p className="text-xs" style={{ color: doc.status === 'error' ? 'var(--color-error)' : 'var(--color-text-primary)' }}>{doc.reason}</p>
      )}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-[var(--color-text-muted)]">
        {passages.length > 0 && <span>{passages.length} passage{passages.length > 1 ? 's' : ''} in conflict</span>}
        <span className="flex items-center gap-1 flex-wrap">
          found by {doc.change_ids.map(id => <ChangeChip key={id} id={id} changes={changes} />)}
        </span>
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
}: {
  result: ImpactResult | null;
  isLoading: boolean;
  error: string;
  accentColor: string;
  exportName: string;
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
                [passageCount, 'passages in conflict', 'var(--color-text-heading)'],
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
                title="One row per passage in conflict, plus the documents and the change list"
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
                result.changes.map(c => <ChangeBlock key={c.id} change={c} docs={visible} changes={changes} />)
              )}
            </div>

          </>
        )}
      </div>
    </div>
  );
}
