import { useState } from 'react';
import { FileText } from 'lucide-react';
import { toast } from 'sonner';
import { downloadPdf } from '@/lib/exportPdf';
import { errorMessage, exportChangeTableExcel } from './api';
import { baseName } from './files';
import { MarkdownResultCard, StructuredResultCard } from './ResultCards';
import type { DiffItem, DocFile, FeedbackProps, ImagePair } from './types';
import { ACCENT, TabSwitch } from './ui';

interface TrackView {
  text: string;
  running: boolean;
  messageId: number | null;
}

type Tab = 'summary' | 'table';

function feedbackFor(track: TrackView, suffix: string, hashes: { old: string; new: string }, ready: boolean): FeedbackProps | undefined {
  if (!track.text || track.running || !ready) return undefined;
  return {
    submissionKey: track.messageId !== null
      ? `mid-${track.messageId}`
      : `hash-${hashes.old.slice(0, 16)}-${hashes.new.slice(0, 16)}${suffix}`,
    messageId: track.messageId,
  };
}

/** Change Summary and Change Table cards; one at a time, full width, with a tab switch when both exist. */
export function AnalysisResults({ summary, table, items, fileType, images, oldDoc, newDoc, hashes }: {
  summary: TrackView;
  table: TrackView;
  items: DiffItem[];
  fileType: string;
  images: ImagePair[];
  oldDoc: DocFile | null;
  newDoc: DocFile | null;
  hashes: { old: string; new: string };
}) {
  const [activeTab, setActiveTab] = useState<Tab>('summary');
  const summaryActive = !!(summary.text || summary.running);
  const tableActive = !!(table.text || table.running);

  if (!summaryActive && !tableActive) return null;
  const bothActive = summaryActive && tableActive;
  const shown: Tab = bothActive ? activeTab : (summaryActive ? 'summary' : 'table');
  const ready = !!oldDoc && !!newDoc;

  const downloadSummary = async () => {
    const base = newDoc ? baseName(newDoc.name) : 'document';
    try {
      await downloadPdf({
        kind: 'change_summary',
        title: base,
        filename: `change_summary_${base}`,
        meta: [
          { label: 'Original (A)', value: oldDoc?.name ?? '' },
          { label: 'Revised (B)', value: newDoc?.name ?? '' },
        ],
        sections: [{ markdown: summary.text }],
      });
    } catch (e) {
      toast.error(`PDF export failed: ${errorMessage(e)}`);
    }
  };

  return (
    <>
      {fileType === 'docx' && (
        <p className="text-xs text-[var(--color-text-muted)] flex items-center gap-1.5">
          <span>⚠️</span>
          <span><strong>Page numbers are approximate</strong> (DOCX limitation) — actual change may be on an adjacent page.</span>
        </p>
      )}
      <div className="space-y-3">
        {bothActive && (
          <TabSwitch
            tabs={[['summary', 'Change Summary'], ['table', 'Change Table']]}
            active={shown}
            accent={ACCENT.blue}
            onChange={setActiveTab}
          />
        )}
        {shown === 'summary' ? (
          <MarkdownResultCard
            title="Change Summary"
            icon={FileText}
            accent={ACCENT.blue}
            content={summary.text}
            isStreaming={summary.running}
            onDownload={downloadSummary}
            feedbackProps={feedbackFor(summary, '-std', hashes, ready)}
          />
        ) : (
          <StructuredResultCard
            items={items}
            isStreaming={table.running}
            accent={ACCENT.blue}
            fileType={fileType}
            imageContext={images}
            onExportExcel={() => exportChangeTableExcel(table.text, newDoc ? baseName(newDoc.name, 'comparison') : 'comparison', fileType, images)}
            feedbackProps={feedbackFor(table, '', hashes, ready)}
          />
        )}
      </div>
    </>
  );
}
