import { ArrowRight, Loader2, RotateCcw } from 'lucide-react';
import { ACCENT, CancelButton, OutlineButton } from './ui';

interface Track {
  text: string;
  running: boolean;
  run: (forceRefresh?: boolean) => void;
  cancel: () => void;
}

const ICON = (running: boolean) => (running ? <Loader2 className="h-4 w-4 animate-spin" /> : <ArrowRight className="h-4 w-4" />);

/** Generate / cancel / re-run buttons; the two outputs are independent. */
export function AnalysisActions({ summary, table, hasSummary, hasTable, canAnalyze, onRunBoth }: {
  summary: Track;
  table: Track;
  hasSummary: boolean;
  hasTable: boolean;
  canAnalyze: boolean;
  onRunBoth: () => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-3">
      {hasSummary && (
        <button
          onClick={() => summary.run()}
          disabled={!canAnalyze}
          className="flex items-center gap-2 px-5 py-2.5 rounded-xl text-sm font-semibold text-white shadow-sm transition-all duration-150 disabled:opacity-40 disabled:cursor-not-allowed disabled:shadow-none cursor-pointer"
          style={{
            background: canAnalyze
              ? 'linear-gradient(135deg, var(--color-accent-primary) 0%, var(--color-accent-secondary) 100%)'
              : 'var(--color-border)',
            boxShadow: canAnalyze ? '0 2px 8px rgba(0,85,164,0.35)' : undefined,
          }}
          title="Narrative diff grouped by section — one bullet per change."
        >
          {ICON(summary.running)}
          {summary.running ? 'Generating…' : 'Generate Change Summary'}
        </button>
      )}
      {summary.running && <CancelButton onClick={summary.cancel} />}
      {summary.text && canAnalyze && (
        <OutlineButton onClick={() => summary.run(true)} title="Force a fresh Change Summary, ignoring cached results">
          <RotateCcw className="h-4 w-4" />
          Re-run
        </OutlineButton>
      )}

      {hasTable && (
        <button
          onClick={() => table.run()}
          disabled={!canAnalyze}
          className="flex items-center gap-2 px-5 py-2.5 rounded-xl text-sm font-semibold transition-all duration-150 disabled:opacity-40 disabled:cursor-not-allowed border cursor-pointer"
          style={{ borderColor: ACCENT.blue, color: canAnalyze ? ACCENT.blue : 'var(--color-text-muted)', background: 'transparent' }}
          title="Structured change table with type and criticality. Exportable to Excel."
        >
          {ICON(table.running)}
          {table.running ? 'Generating…' : 'Generate Change Table'}
        </button>
      )}
      {table.running && <CancelButton onClick={table.cancel} />}
      {table.text && canAnalyze && (
        <OutlineButton onClick={() => table.run(true)} title="Force a fresh Change Table, ignoring cached results">
          <RotateCcw className="h-4 w-4" />
          Re-run
        </OutlineButton>
      )}

      {hasSummary && hasTable && (
        <OutlineButton onClick={onRunBoth} disabled={!canAnalyze}>
          <ArrowRight className="h-4 w-4" />
          Run Both
        </OutlineButton>
      )}
    </div>
  );
}
