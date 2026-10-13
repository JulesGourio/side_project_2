import { Download, FileText, Loader2 } from 'lucide-react';
import { DiffItem, FeedbackProps, ImagePair } from './compareShared';
import { FeedbackCommentBox, FeedbackThumbs, useCardFeedback } from './CardFeedback';
import { JsonDiffTable } from './JsonDiffTable';

export function StructuredResultCard({
  items,
  isStreaming,
  accentColor,
  fileType,
  imageContext = [],
  onExportExcel,
  feedbackProps,
}: {
  items: DiffItem[];
  isStreaming: boolean;
  accentColor: string;
  fileType?: string;
  imageContext?: ImagePair[];
  onExportExcel: () => void;
  feedbackProps?: FeedbackProps;
}) {
  const feedback = useCardFeedback(feedbackProps);

  return (
    <div className="rounded-2xl border border-[var(--color-border)]/40 bg-[var(--color-background)] shadow-sm overflow-hidden">
      <div className="h-0.5 w-full" style={{ background: accentColor }} />
      <div className="flex items-center justify-between px-5 py-4 border-b border-[var(--color-border)]/30 bg-[var(--color-bg-secondary)]/40 gap-3">
        <div className="flex items-center gap-2.5 min-w-0 flex-1">
          <div className="flex-shrink-0 w-7 h-7 rounded-lg flex items-center justify-center" style={{ background: `${accentColor}18` }}>
            <FileText className="h-3.5 w-3.5" style={{ color: accentColor }} />
          </div>
          <span className="text-sm font-semibold text-[var(--color-text-heading)] truncate">Change Table</span>
          {isStreaming && (
            <span className="flex-shrink-0 flex items-center gap-1.5 text-xs text-[var(--color-text-muted)]">
              <Loader2 className="h-3 w-3 animate-spin" style={{ color: accentColor }} />
              Generating…
            </span>
          )}
        </div>

        <div className="flex-shrink-0 flex items-center gap-3">
          {!isStreaming && items.length > 0 && (
            <span className="text-xs text-[var(--color-text-muted)]">{items.length} change{items.length !== 1 ? 's' : ''}</span>
          )}

          <FeedbackThumbs feedback={feedback} />

          {!isStreaming && items.length > 0 && (
            <button
              onClick={onExportExcel}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-all cursor-pointer"
              style={{ color: accentColor }}
              onMouseEnter={e => (e.currentTarget.style.background = `${accentColor}12`)}
              onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
              title="Export analysis as Excel workbook"
            >
              <Download className="h-3.5 w-3.5" />
              Export Excel
            </button>
          )}
        </div>
      </div>

      {/* Comment bar — slides in after a thumb is selected */}
      <FeedbackCommentBox feedback={feedback} />

      <JsonDiffTable items={items} isStreaming={isStreaming} accentColor={accentColor} fileType={fileType} imageContext={imageContext} />
    </div>
  );
}
