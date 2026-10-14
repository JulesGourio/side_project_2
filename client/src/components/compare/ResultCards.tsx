import type { ElementType } from 'react';
import { FileText } from 'lucide-react';
import { MarkdownRenderer } from '@/components/shared/MarkdownRenderer';
import type { DiffItem, DocSummaryState, FeedbackProps, ImagePair } from './types';
import { FeedbackCommentBox, FeedbackThumbs, useCardFeedback } from './Feedback';
import { JsonDiffTable } from './JsonDiffTable';
import { CardShell, DownloadButton, GeneratingBadge, Spinner, useStickToBottom } from './ui';

const BODY_CLASS = 'p-5 max-h-[650px] overflow-y-auto text-sm leading-relaxed';

function Pending({ accent, label }: { accent: string; label: string }) {
  return (
    <div className="flex items-center gap-2 text-[var(--color-text-muted)]">
      <Spinner accent={accent} />
      <span className="italic text-sm">{label}</span>
    </div>
  );
}

export function MarkdownResultCard({ title, icon, accent, content, isStreaming, onDownload, feedbackProps }: {
  title: string;
  icon: ElementType;
  accent: string;
  content: string;
  isStreaming: boolean;
  onDownload: () => void;
  feedbackProps?: FeedbackProps;
}) {
  const feedback = useCardFeedback(feedbackProps);
  const bodyRef = useStickToBottom<HTMLDivElement>(isStreaming, content, 60);

  return (
    <CardShell
      icon={icon}
      title={title}
      accent={accent}
      status={isStreaming && <GeneratingBadge accent={accent} />}
      actions={
        <>
          <FeedbackThumbs feedback={feedback} />
          {content && !isStreaming && <DownloadButton accent={accent} label="Download PDF" title="Download as PDF" onClick={onDownload} />}
        </>
      }
    >
      <FeedbackCommentBox feedback={feedback} />
      <div ref={bodyRef} className={BODY_CLASS}>
        {content ? <MarkdownRenderer content={content} /> : <Pending accent={accent} label="Waiting for response…" />}
      </div>
    </CardShell>
  );
}

export function StructuredResultCard({ items, isStreaming, accent, fileType, imageContext, onExportExcel, feedbackProps }: {
  items: DiffItem[];
  isStreaming: boolean;
  accent: string;
  fileType: string;
  imageContext: ImagePair[];
  onExportExcel: () => void;
  feedbackProps?: FeedbackProps;
}) {
  const feedback = useCardFeedback(feedbackProps);
  const done = !isStreaming && items.length > 0;

  return (
    <CardShell
      icon={FileText}
      title="Change Table"
      accent={accent}
      status={isStreaming && <GeneratingBadge accent={accent} />}
      actions={
        <>
          {done && <span className="text-xs text-[var(--color-text-muted)]">{items.length} change{items.length !== 1 ? 's' : ''}</span>}
          <FeedbackThumbs feedback={feedback} />
          {done && <DownloadButton accent={accent} label="Export Excel" title="Export analysis as Excel workbook" onClick={onExportExcel} />}
        </>
      }
    >
      <FeedbackCommentBox feedback={feedback} />
      <JsonDiffTable items={items} isStreaming={isStreaming} accentColor={accent} fileType={fileType} imageContext={imageContext} />
    </CardShell>
  );
}

export function DocSummaryCard({ title, state, accent, onDownload }: {
  title: string;
  state: DocSummaryState;
  accent: string;
  onDownload: () => void;
}) {
  const { text, error, loading, noContent, meta } = state;
  const italicNote = 'text-sm italic text-[var(--color-text-muted)]';

  return (
    <CardShell
      icon={FileText}
      title={title}
      accent={accent}
      status={loading && <GeneratingBadge accent={accent} label="Summarizing…" />}
      actions={!loading && text && <DownloadButton accent={accent} label="Download PDF" title="Download as PDF" onClick={onDownload} />}
    >
      {meta?.truncated && (
        <div className="px-5 py-2 text-xs" style={{ color: '#d97706', background: '#fffbeb' }}>
          ⚠️ Document text was truncated before summarizing — result may be partial.
        </div>
      )}
      <div className={BODY_CLASS}>
        {loading && !text ? (
          <Pending accent={accent} label="Summarizing…" />
        ) : error ? (
          <p className="text-sm" style={{ color: 'var(--color-error)' }}>{error}</p>
        ) : text ? (
          <MarkdownRenderer content={text} />
        ) : noContent ? (
          <p className={italicNote}>
            No extractable text found in this document (e.g. a scanned page with no text layer) — nothing to summarize.
          </p>
        ) : (
          <p className={italicNote}>No summary yet.</p>
        )}
      </div>
    </CardShell>
  );
}
