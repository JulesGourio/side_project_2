import { useEffect, useRef } from 'react';
import { Download, Loader2 } from 'lucide-react';
import { MarkdownRenderer } from '@/components/shared/MarkdownRenderer';
import { FeedbackProps } from './compareShared';
import { FeedbackCommentBox, FeedbackThumbs, useCardFeedback } from './CardFeedback';

export function ResultCard({
  title,
  icon: Icon,
  accentColor,
  content,
  isStreaming,
  onDownload,
  feedbackProps,
}: {
  title: string;
  icon: React.ElementType;
  accentColor: string;
  content: string;
  isStreaming: boolean;
  onDownload: () => void;
  feedbackProps?: FeedbackProps;
}) {
  const bottomRef = useRef<HTMLDivElement>(null);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const feedback = useCardFeedback(feedbackProps);

  // Auto-scroll only if the user is already at (or near) the bottom.
  // Uses scrollTop directly — no smooth animation that fights manual scrolling.
  useEffect(() => {
    if (!isStreaming) return;
    const el = scrollContainerRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (distanceFromBottom < 60) {
      el.scrollTop = el.scrollHeight;
    }
  }, [content, isStreaming]);

  return (
    <div className="rounded-2xl border border-[var(--color-border)]/40 bg-[var(--color-background)] shadow-sm overflow-hidden">
      {/* Colored top accent bar */}
      <div className="h-0.5 w-full" style={{ background: accentColor }} />

      {/* Header */}
      <div className="flex items-center justify-between px-5 py-4 border-b border-[var(--color-border)]/30 bg-[var(--color-bg-secondary)]/40 gap-3">
        <div className="flex items-center gap-2.5 min-w-0 flex-1">
          <div className="flex-shrink-0 w-7 h-7 rounded-lg flex items-center justify-center" style={{ background: `${accentColor}18` }}>
            <Icon className="h-3.5 w-3.5" style={{ color: accentColor }} />
          </div>
          <span className="text-sm font-semibold text-[var(--color-text-heading)] truncate">{title}</span>
          {isStreaming && (
            <span className="flex-shrink-0 flex items-center gap-1.5 text-xs text-[var(--color-text-muted)]">
              <Loader2 className="h-3 w-3 animate-spin" style={{ color: accentColor }} />
              Generating…
            </span>
          )}
        </div>

        <div className="flex-shrink-0 flex items-center gap-3">
          <FeedbackThumbs feedback={feedback} />

          {content && !isStreaming && (
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
      </div>

      {/* Comment bar — slides in after a thumb is selected */}
      <FeedbackCommentBox feedback={feedback} />

      {/* Content */}
      <div ref={scrollContainerRef} className="p-5 max-h-[650px] overflow-y-auto text-sm leading-relaxed">
        {content ? (
          <MarkdownRenderer content={content} />
        ) : (
          <div className="flex items-center gap-2 text-[var(--color-text-muted)]">
            <Loader2 className="h-4 w-4 animate-spin" style={{ color: accentColor }} />
            <span className="italic text-sm">Waiting for response…</span>
          </div>
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
