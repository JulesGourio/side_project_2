import { Download, FileText, Loader2 } from 'lucide-react';
import { MarkdownRenderer } from '@/components/shared/MarkdownRenderer';
import { DocSummaryState } from './compareShared';

export function DocSummaryCard({
  title,
  state,
  accentColor,
  onDownload,
}: {
  title: string;
  state: DocSummaryState;
  accentColor: string;
  onDownload: () => void;
}) {
  const { text, error, loading, noContent, meta } = state;
  return (
    <div className="rounded-2xl border border-[var(--color-border)]/40 bg-[var(--color-background)] shadow-sm overflow-hidden">
      <div className="h-0.5 w-full" style={{ background: accentColor }} />

      <div className="flex items-center justify-between px-5 py-4 border-b border-[var(--color-border)]/30 bg-[var(--color-bg-secondary)]/40">
        <div className="flex items-center gap-2.5">
          <div className="w-7 h-7 rounded-lg flex items-center justify-center" style={{ background: `${accentColor}18` }}>
            <FileText className="h-3.5 w-3.5" style={{ color: accentColor }} />
          </div>
          <span className="text-sm font-semibold text-[var(--color-text-heading)]">{title}</span>
          {loading && (
            <span className="flex items-center gap-1.5 text-xs text-[var(--color-text-muted)]">
              <Loader2 className="h-3 w-3 animate-spin" style={{ color: accentColor }} />
              Summarizing…
            </span>
          )}
        </div>
        {!loading && text && (
          <button
            onClick={onDownload}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-all cursor-pointer"
            style={{ color: accentColor }}
            onMouseEnter={e => (e.currentTarget.style.background = `${accentColor}12`)}
            onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
            title="Download as PDF"
          >
            <Download className="h-3.5 w-3.5" />
            Download PDF
          </button>
        )}
      </div>

      {meta?.truncated && (
        <div className="px-5 py-2 text-xs" style={{ color: '#d97706', background: '#fffbeb' }}>
          ⚠️ Document text was truncated before summarizing — result may be partial.
        </div>
      )}

      <div className="p-5 max-h-[650px] overflow-y-auto text-sm leading-relaxed">
        {loading && !text ? (
          <div className="flex items-center gap-2 text-[var(--color-text-muted)]">
            <Loader2 className="h-4 w-4 animate-spin" style={{ color: accentColor }} />
            <span className="italic text-sm">Summarizing…</span>
          </div>
        ) : error ? (
          <p className="text-sm" style={{ color: 'var(--color-error)' }}>{error}</p>
        ) : text ? (
          <MarkdownRenderer content={text} />
        ) : noContent ? (
          <p className="text-sm italic text-[var(--color-text-muted)]">
            No extractable text found in this document (e.g. a scanned page with no text layer) — nothing to summarize.
          </p>
        ) : (
          <p className="text-sm italic text-[var(--color-text-muted)]">No summary yet.</p>
        )}
      </div>
    </div>
  );
}
