import { useMemo, useState, type ReactNode } from 'react';
import { Database, Download, Loader2 } from 'lucide-react';
import { downloadExport } from '../api';
import { CardShell, GeneratingBadge, Spinner } from '../ui';
import { FeedbackContext, SearchFeedback, type FeedbackTarget } from './ImpactFeedback';
import { ChangeLinkContext, ChangeList, DocCard } from './ImpactParts';
import { sortImpactDocs, type ChangesById, type ImpactResult } from './types';

type StatusFilter = 'all' | 'impacted' | 'check' | 'not_impacted';
type View = 'doc' | 'change';

const STATUS_FILTERS: [StatusFilter, string][] = [['all', 'All'], ['impacted', 'Impacted'], ['check', 'To check'], ['not_impacted', 'Not impacted']];

function SegButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: ReactNode }) {
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

export function ImpactResultsCard({ result, isLoading, error, accentColor, exportName, changeTableShown, messageId, oldFileHash, newFileHash }: {
  result: ImpactResult | null;
  isLoading: boolean;
  error: string;
  accentColor: string;
  exportName: string;
  /** The Change Table this result was computed from is on screen: change ids link to its rows. */
  changeTableShown: boolean;
  messageId: number | null;
  oldFileHash: string;
  newFileHash: string;
}) {
  const [view, setView] = useState<View>('doc');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
  const [exporting, setExporting] = useState(false);

  const changes = useMemo<ChangesById>(
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
    const form = new FormData();
    form.append('result_json', JSON.stringify({ ...result, documents: docs }));
    form.append('filename', exportName);
    await downloadExport('/api/compare/impact/export-excel', form, `${exportName}.xlsx`, 'Excel');
    setExporting(false);
  };

  const stats: [number, string, string][] = [
    [count('impacted'), 'impacted', '#16a34a'],
    [count('check'), 'to check', '#d97706'],
    [count('not_impacted'), 'not impacted', 'var(--color-text-muted)'],
    [passageCount, 'passages to update', 'var(--color-text-heading)'],
  ];

  return (
    <ChangeLinkContext.Provider value={changeTableShown && result?.source === 'structured'}>
      <FeedbackContext.Provider value={feedbackTarget}>
        <CardShell
          icon={Database}
          title="Impacted Docs"
          accent={accentColor}
          status={isLoading && (
            <GeneratingBadge accent={accentColor} label={result ? `Reviewing documents… ${docs.length}/${result.candidates}` : 'Searching the knowledge base…'} />
          )}
        >
          <div className="p-5 space-y-4 text-sm">
            {error ? (
              <p className="text-sm" style={{ color: 'var(--color-error)' }}>{error}</p>
            ) : !result ? (
              <div className="flex items-center gap-2 text-[var(--color-text-muted)]">
                <Spinner accent={accentColor} />
                <span className="italic text-sm">Searching the knowledge base…</span>
              </div>
            ) : result.no_changes ? (
              <p className="text-sm italic text-[var(--color-text-muted)]">The analysis reported no substantive change — nothing to search.</p>
            ) : (
              <>
                <div className="flex flex-wrap items-baseline gap-x-6 gap-y-1">
                  {stats.map(([n, label, color]) => (
                    <span key={label} className="text-xs text-[var(--color-text-muted)]">
                      <b className="text-lg mr-1 tabular-nums" style={{ color }}>{n}</b>{label}
                    </span>
                  ))}
                </div>
                {(result.changes_not_searched?.length ?? 0) > 0 && (
                  <p className="text-xs" style={{ color: 'var(--color-error)' }}>
                    {result.changes_not_searched!.length} change{result.changes_not_searched!.length > 1 ? 's were' : ' was'} not searched
                    ({result.changes_not_searched!.join(', ')}): too many changes for one search. Impacts of these changes may be missing.
                  </p>
                )}
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
                  {STATUS_FILTERS.map(([k, label]) => (
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
        </CardShell>
      </FeedbackContext.Provider>
    </ChangeLinkContext.Provider>
  );
}
