import { FileText, Loader2, RotateCcw } from 'lucide-react';
import { DocDropZone } from './DocDropZone';
import type { useDocSummaries } from './hooks/useDocSummaries';
import { DocSummaryCard } from './ResultCards';
import type { DocFile, DocSide } from './types';
import { ACCENT, AccentButton, OutlineButton, TabSwitch } from './ui';

type Summaries = ReturnType<typeof useDocSummaries>;

const SIDES: { side: DocSide; label: string; step: number; short: string }[] = [
  { side: 'old', label: 'Document A (Old)', step: 1, short: 'Document A' },
  { side: 'new', label: 'Document B (New)', step: 2, short: 'Document B' },
];

export function DocsPanel({ docs, busy, summaries, onFile, onInvalidFile }: {
  docs: Record<DocSide, DocFile | null>;
  busy: boolean;
  summaries: Summaries;
  onFile: (side: DocSide, file: DocFile | null) => void;
  onInvalidFile: (message: string) => void;
}) {
  const state = summaries.summaries;
  const isActive = (side: DocSide) => {
    const s = state[side];
    return !!(s.text || s.loading || s.error || s.noContent);
  };
  const oldActive = isActive('old');
  const newActive = isActive('new');
  // Exactly one card, full width: a tab switch when both are active rather than two half-size cards.
  const shown: DocSide = oldActive && newActive ? summaries.activeSide : (oldActive ? 'old' : 'new');
  const shownLabel = SIDES.find(s => s.side === shown)!.short;

  return (
    <>
      <div className="flex gap-4">
        {SIDES.map(({ side, label, step }) => (
          <DocDropZone
            key={side}
            label={label}
            step={step}
            file={docs[side]}
            onFile={f => onFile(side, f)}
            onRemove={() => onFile(side, null)}
            onInvalidFile={onInvalidFile}
            disabled={busy}
          />
        ))}
      </div>

      {(docs.old || docs.new) && (
        <div className="flex flex-wrap items-center gap-3">
          {SIDES.filter(({ side }) => docs[side]).map(({ side, short }) => (
            <div key={side} className="contents">
              <AccentButton
                accent={ACCENT.green}
                onClick={() => summaries.summarize(side)}
                disabled={state[side].loading}
                title={`Summarize ${short} on its own — no comparison needed`}
              >
                {state[side].loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileText className="h-4 w-4" />}
                {state[side].loading ? 'Summarizing…' : `Summarize ${short}`}
              </AccentButton>
              {state[side].text && !state[side].loading && (
                <OutlineButton onClick={() => summaries.summarize(side, true)} title={`Force a fresh summary of ${short}, ignoring cached results`}>
                  <RotateCcw className="h-4 w-4" />
                  Re-run
                </OutlineButton>
              )}
            </div>
          ))}
        </div>
      )}

      {(oldActive || newActive) && (
        <div className="space-y-3">
          {oldActive && newActive && (
            <TabSwitch
              tabs={[['old', 'Document A'], ['new', 'Document B']]}
              active={shown}
              accent={ACCENT.green}
              onChange={summaries.setActiveSide}
            />
          )}
          <DocSummaryCard
            title={`${shownLabel} Summary`}
            state={state[shown]}
            accent={ACCENT.green}
            onDownload={() => summaries.download(shown)}
          />
        </div>
      )}
    </>
  );
}
