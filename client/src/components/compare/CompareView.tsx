import { useMemo, useState } from 'react';
import { AlertCircle, History, Trash2 } from 'lucide-react';
import { toast } from 'sonner';
import { WhatsNewButton, WhatsNewModal } from '@/components/layout/WhatsNewBanner';
import { AnalysisActions } from './AnalysisActions';
import { AnalysisResults } from './AnalysisResults';
import { autoSaveExcel, autoSavePdf, parsePartialJsonItems } from './api';
import { ComparisonHistory } from './ComparisonHistory';
import { DocsPanel } from './DocsPanel';
import { useAnalysisTrack } from './hooks/useAnalysisTrack';
import { useCompareConfig } from './hooks/useCompareConfig';
import { useCompareDocs } from './hooks/useCompareDocs';
import { useDocSummaries } from './hooks/useDocSummaries';
import { useHistoryLoad } from './hooks/useHistoryLoad';
import { useImpactSearch } from './hooks/useImpactSearch';
import { ImpactSection } from './ImpactSection';
import { LS } from './storage';
import type { DocFile, DocSide, ImagePair } from './types';

export function CompareView() {
  const docCtl = useCompareDocs();
  const { docs, hashes } = docCtl;
  const [error, setError] = useState('');
  const [fileType, setFileType] = useState('');
  const [images, setImages] = useState<ImagePair[]>([]);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [showWhatsNew, setShowWhatsNew] = useState(false);

  const config = useCompareConfig(docs.old?.name || docs.new?.name);
  const summaries = useDocSummaries(docs);
  const impact = useImpactSearch();

  const trackBase = {
    oldDoc: docs.old,
    newDoc: docs.new,
    volumeConfigured: config.volumeConfigured,
    onHashes: docCtl.setHashes,
    onError: setError,
    onFileType: setFileType,
  };
  const table = useAnalysisTrack({
    ...trackBase,
    version: 'structured',
    label: 'Change Table',
    lsText: LS.analysis,
    lsMessageId: LS.messageId,
    onBegin: () => { setError(''); impact.reset(); setImages([]); },
    onImages: setImages,
    onSaved: s => autoSaveExcel(s.sessionPath, s.text, s.fileType, s.images),
  });
  const summary = useAnalysisTrack({
    ...trackBase,
    version: 'standard',
    label: 'Change Summary',
    lsText: LS.analysisStd,
    lsMessageId: LS.messageIdStd,
    onBegin: () => { setError(''); impact.reset(); },
    onSaved: s => autoSavePdf(s.sessionPath, s.text, docs.old?.name ?? '', docs.new?.name ?? ''),
  });

  const items = useMemo(() => parsePartialJsonItems(table.text), [table.text]);
  const handleLoadFromHistory = useHistoryLoad({ docs: docCtl, structured: table, standard: summary, impact, setError });

  const busy = table.running || summary.running || impact.running;
  const canAnalyze = !!docs.old?.data && !!docs.new?.data && !busy;
  // The Change Table is preferred; the Change Summary is an equally usable changes summary.
  const changesText = table.text || summary.text;
  const canSearch = !!changesText && !table.running && !summary.running;

  const clearAnalysis = () => {
    table.reset();
    summary.reset();
    impact.reset();
    docCtl.clearHashes();
    setError('');
  };

  const handleFile = (side: DocSide, file: DocFile | null) => {
    clearAnalysis();
    summaries.clear(side);
    docCtl.setDoc(side, file);
  };

  const clearAll = () => {
    clearAnalysis();
    summaries.clear();
    docCtl.setDoc('old', null);
    docCtl.setDoc('new', null);
  };

  const runBoth = () => { void Promise.allSettled([table.run(), summary.run()]); };

  const runImpact = (forceRefresh = false) => impact.run({
    changesText,
    oldHash: hashes.old,
    newHash: hashes.new,
    oldName: docs.old?.name ?? '',
    newName: docs.new?.name ?? '',
    // Kept with the comparison it was run on, so reopening the history entry shows it again.
    historyId: table.text ? table.messageId : summary.messageId,
  }, forceRefresh);

  return (
    <div className="max-w-7xl mx-auto px-4 py-10 space-y-8">
      <div className="flex items-start justify-between gap-4">
        <div className="space-y-1">
          <h1 className="text-2xl font-bold tracking-tight" style={{ color: 'var(--color-text-heading)', fontFamily: 'var(--font-heading)' }}>
            Document Comparison
          </h1>
          <p className="text-sm" style={{ color: 'var(--color-text-muted)' }}>
            Upload two documents (PDF, DOCX, PPTX, Excel, image…) to detect differences.
          </p>
          <p className="text-sm font-semibold" style={{ color: 'var(--color-text-heading)' }}>
            This tool is intended to compare two versions of the same document.
          </p>
        </div>
        <div className="flex-shrink-0 flex items-center gap-2">
          <WhatsNewButton tabId="compare" open={showWhatsNew} onClick={() => setShowWhatsNew(o => !o)} />
          {(docs.old || docs.new || table.text || summary.text) && (
            <button
              onClick={clearAll}
              disabled={busy}
              className="flex items-center gap-1.5 px-3 py-2 rounded-xl text-sm font-medium border transition-all cursor-pointer disabled:opacity-40 border-[var(--color-border)] text-[var(--color-text-muted)] hover:border-[var(--color-error)] hover:text-[var(--color-error)]"
            >
              <Trash2 className="h-4 w-4" />
              Clear all
            </button>
          )}
          <button
            onClick={() => setHistoryOpen(true)}
            className="flex items-center gap-2 px-3.5 py-2 rounded-xl text-sm font-medium border transition-all cursor-pointer border-[var(--color-border)] text-[var(--color-text-muted)] hover:border-[var(--color-accent-primary)] hover:text-[var(--color-accent-primary)]"
          >
            <History className="h-4 w-4" />
            History
          </button>
        </div>
      </div>

      {showWhatsNew && <WhatsNewModal tabId="compare" onClose={() => setShowWhatsNew(false)} />}

      <ComparisonHistory open={historyOpen} onClose={() => setHistoryOpen(false)} onLoad={handleLoadFromHistory} />

      {error && (
        <div className="flex items-start gap-3 px-4 py-3.5 rounded-xl border border-[var(--color-error)]/25 bg-[var(--color-error)]/5">
          <AlertCircle className="h-4 w-4 text-[var(--color-error)] mt-0.5 flex-shrink-0" />
          <p className="text-sm text-[var(--color-error)] leading-relaxed">{error}</p>
        </div>
      )}

      <div className="rounded-2xl border border-[var(--color-border)]/40 bg-[var(--color-bg-secondary)]/30 p-6 space-y-5">
        <DocsPanel
          docs={docs}
          busy={busy}
          summaries={summaries}
          onFile={handleFile}
          onInvalidFile={message => { toast.error(message); setError(message); }}
        />
        <div className="border-t border-[var(--color-border)]/30" />
        <AnalysisActions
          summary={summary}
          table={table}
          hasSummary={config.hasStandard}
          hasTable={config.hasStructured}
          canAnalyze={canAnalyze}
          onRunBoth={runBoth}
        />
      </div>

      <AnalysisResults
        summary={summary}
        table={table}
        items={items}
        fileType={fileType}
        images={images}
        oldDoc={docs.old}
        newDoc={docs.new}
        hashes={hashes}
      />

      <ImpactSection
        configured={config.impactConfigured}
        result={impact.result}
        error={impact.error}
        running={impact.running}
        usingTable={!!table.text}
        hasBothAnalyses={!!table.text && !!summary.text}
        canSearch={canSearch}
        changeTableShown={items.length > 0 && !table.running}
        messageId={table.text ? table.messageId : summary.messageId}
        hashes={hashes}
        names={{ old: docs.old?.name ?? '', new: docs.new?.name ?? '' }}
        onRun={runImpact}
        onCancel={impact.cancel}
      />
    </div>
  );
}
