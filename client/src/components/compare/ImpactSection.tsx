import { Database, RotateCcw } from 'lucide-react';
import { ImpactResultsCard } from './impact/ImpactResultsCard';
import type { ImpactResult } from './impact/types';
import { baseName } from './files';
import { ACCENT, AccentButton, CancelButton, OutlineButton, Spinner } from './ui';

export function ImpactSection({ configured, result, error, running, usingTable, hasBothAnalyses, canSearch, changeTableShown, messageId, hashes, names, onRun, onCancel }: {
  /** The impact index is set up on the server; without it only a restored result is shown. */
  configured: boolean;
  result: ImpactResult | null;
  error: string;
  running: boolean;
  /** The Change Table feeds the search (it takes priority over the Change Summary). */
  usingTable: boolean;
  hasBothAnalyses: boolean;
  /** There is a change text to search with and no analysis is still streaming. */
  canSearch: boolean;
  changeTableShown: boolean;
  messageId: number | null;
  hashes: { old: string; new: string };
  names: { old: string; new: string };
  onRun: (forceRefresh?: boolean) => void;
  onCancel: () => void;
}) {
  return (
    <>
      {configured && <div className="space-y-3">
        <div className="rounded-xl border px-4 py-2.5 text-xs" style={{ borderColor: ACCENT.amber, background: '#fffbeb', color: '#92400e' }}>
          This section is in testing phase — results may be incomplete or inaccurate.
        </div>
        <h2 className="text-sm font-semibold" style={{ color: 'var(--color-text-heading)' }}>Impact Search</h2>
        {canSearch && (
          <p className="text-xs -mb-1" style={{ color: 'var(--color-text-muted)' }}>
            Using: <strong>{usingTable ? 'Change Table' : 'Change Summary'}</strong> as the change summary
            {hasBothAnalyses && ' (Change Table takes priority when both are available)'}
          </p>
        )}
      </div>}

      {configured && canSearch && (
        <div className="flex flex-wrap items-center gap-3">
          <AccentButton accent={ACCENT.amber} onClick={() => onRun()} disabled={running}>
            {running ? <Spinner accent="currentColor" /> : <Database className="h-4 w-4" />}
            {running ? 'Judging…' : 'Judge Impacted Docs'}
          </AccentButton>
          {running && <CancelButton onClick={onCancel} />}
          {result?.done && !running && (
            <OutlineButton onClick={() => onRun(true)} title="Force a fresh search, ignoring cached results">
              <RotateCcw className="h-4 w-4" />
              Re-run
            </OutlineButton>
          )}
        </div>
      )}

      {(result || running || error) && (
        <ImpactResultsCard
          result={result}
          isLoading={running}
          error={error}
          accentColor={ACCENT.amber}
          changeTableShown={changeTableShown}
          messageId={messageId}
          oldFileHash={hashes.old}
          newFileHash={hashes.new}
          exportName={`impact_${baseName(names.new || names.old || 'search', 'search')}`}
        />
      )}
    </>
  );
}
