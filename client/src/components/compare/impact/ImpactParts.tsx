import { createContext, useContext, useState, type ReactNode } from 'react';
import { ExternalLink } from 'lucide-react';
import { VerdictVote } from './ImpactFeedback';
import { FOCUS_CHANGE_EVENT, statusStyle, type ChangesById, type ImpactChange, type ImpactDoc, type ImpactPassage } from './types';

const CRIT_COLOR: Record<string, string> = { high: '#dc2626', medium: '#d97706', low: '#6b7280' };
const ARCHIVE_COLOR = '#a16207';

/** True when the change ids of this result are the row numbers of the Change Table on screen. */
export const ChangeLinkContext = createContext(false);

function Chip({ children, color, title }: { children: ReactNode; color: string; title?: string }) {
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

function ChangeChip({ id, changes }: { id: string; changes: ChangesById }) {
  const linked = useContext(ChangeLinkContext);
  const className = 'inline-flex px-1.5 py-0.5 rounded text-[10px] font-mono font-medium';
  const style = { color: 'var(--color-accent-primary)', background: 'color-mix(in srgb, var(--color-accent-primary) 12%, transparent)' };
  if (!linked) return <span title={changes[id]?.text ?? id} className={className} style={style}>{id}</span>;
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

function BeforeAfter({ change }: { change: ImpactChange }) {
  return (
    <>
      {change.before && <><del style={{ color: 'var(--color-error)' }}>{change.before}</del> → </>}
      <span className="font-semibold" style={{ color: '#16a34a' }}>{change.after}</span>
    </>
  );
}

/** The change a passage conflicts with, spelled out: a bare "C37" means nothing when the comparison has dozens. */
export function ChangeLine({ id, changes }: { id: string; changes: ChangesById }) {
  const c = changes[id];
  if (!c) return <ChangeChip id={id} changes={changes} />;
  return (
    <div className="flex flex-wrap items-baseline gap-x-1.5 text-xs break-words">
      <ChangeChip id={id} changes={changes} />
      {c.section && <span className="font-semibold" style={{ color: 'var(--color-text-heading)' }}>{c.section}</span>}
      {c.before || c.after ? <span><BeforeAfter change={c} /></span> : <span className="line-clamp-2">{c.summary || c.text}</span>}
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
  return <Chip color={ARCHIVE_COLOR} title="Published before 2018">Archive {doc.doc_date.slice(0, 4)}</Chip>;
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

function PassageView({ p, changes }: { p: ImpactPassage; changes: ChangesById }) {
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

export function DocCard({ doc, changes }: { doc: ImpactDoc; changes: ChangesById }) {
  const st = statusStyle(doc.status);
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

function ChangeBlock({ change, docs, changes }: { change: ImpactChange; docs: ImpactDoc[]; changes: ChangesById }) {
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
        {change.before || change.after ? <BeforeAfter change={change} /> : change.summary || change.text}
      </p>
      {!change.searched ? (
        <p className="text-xs italic text-[var(--color-text-muted)]">Editorial change — not searched.</p>
      ) : hits.length === 0 ? (
        <p className="text-xs italic text-[var(--color-text-muted)]">No judged document conflicts with this change.</p>
      ) : (
        <div className="space-y-2 pl-3 border-l-2 border-[var(--color-border)]/60">
          {hits.map(({ d, p }) => (
            <div key={`${d.ref}-${p.chunk_id}`} className="space-y-0.5">
              <div className="flex flex-wrap items-center gap-1.5">
                <DocLink doc={d} />
                <ArchiveChip doc={d} />
                <Chip color={statusStyle(d.status).color}>{statusStyle(d.status).label}</Chip>
                {p.section && <span className="font-mono text-[11px]" style={{ color: 'var(--color-text-heading)' }}>{p.section}</span>}
              </div>
              {p.quote && <p className="text-[13px] break-words">« {p.quote} »</p>}
              {p.explanation && <p className="text-xs text-[var(--color-text-muted)]">→ {p.explanation}</p>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * Changes that conflict with at least one document first, in full; the others
 * (often the vast majority on a heavily revised document) folded into one line.
 */
export function ChangeList({ changeList, docs, changes }: { changeList: ImpactChange[]; docs: ImpactDoc[]; changes: ChangesById }) {
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
