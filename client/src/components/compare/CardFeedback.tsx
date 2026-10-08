import { useEffect, useState } from 'react';
import { CheckCircle2, Send, ThumbsDown, ThumbsUp } from 'lucide-react';
import { FeedbackProps, isFeedbackSubmitted, markFeedbackSubmitted } from './compareShared';

/** Thumbs-up / thumbs-down vote with an optional comment on a result card, sent once per comparison. */
export function useCardFeedback(feedbackProps?: FeedbackProps) {
  const [vote, setVote] = useState<'up' | 'down' | null>(null);
  const [comment, setComment] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [done, setDone] = useState(false);

  useEffect(() => {
    if (!feedbackProps) return;
    if (isFeedbackSubmitted(feedbackProps.submissionKey)) {
      setDone(true);
    } else {
      setDone(false);
      setVote(null);
      setComment('');
    }
  }, [feedbackProps?.submissionKey]);

  const submit = async () => {
    if (!vote || submitting || !feedbackProps) return;
    setSubmitting(true);
    try {
      await fetch('/api/feedback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ vote, comment: comment.trim() || null, message_id: feedbackProps.messageId ?? null }),
      });
      markFeedbackSubmitted(feedbackProps.submissionKey);
    } catch { /* best-effort */ }
    finally { setSubmitting(false); setDone(true); }
  };

  return { enabled: !!feedbackProps, vote, setVote, comment, setComment, submitting, done, submit };
}

export type CardFeedbackState = ReturnType<typeof useCardFeedback>;

export function FeedbackThumbs({ feedback }: { feedback: CardFeedbackState }) {
  if (!feedback.enabled) return null;
  if (feedback.done) {
    return (
      <div className="flex items-center gap-1.5" style={{ color: 'var(--color-success)' }}>
        <CheckCircle2 className="h-3.5 w-3.5" />
        <span className="text-xs font-medium">Sent</span>
      </div>
    );
  }
  const { vote, setVote } = feedback;
  return (
    <div className="flex items-center gap-1.5">
      <span className="text-xs font-medium" style={{ color: 'var(--color-text-muted)' }}>Feedback</span>
      <button
        onClick={() => setVote(v => v === 'up' ? null : 'up')}
        title="Helpful"
        className="w-7 h-7 rounded-lg flex items-center justify-center border transition-all cursor-pointer"
        style={{
          borderColor: vote === 'up' ? '#16a34a' : 'var(--color-border)',
          color: vote === 'up' ? '#16a34a' : 'var(--color-text-muted)',
          background: vote === 'up' ? '#16a34a12' : 'transparent',
        }}
      >
        <ThumbsUp className="h-3.5 w-3.5" />
      </button>
      <button
        onClick={() => setVote(v => v === 'down' ? null : 'down')}
        title="Not helpful"
        className="w-7 h-7 rounded-lg flex items-center justify-center border transition-all cursor-pointer"
        style={{
          borderColor: vote === 'down' ? '#dc2626' : 'var(--color-border)',
          color: vote === 'down' ? '#dc2626' : 'var(--color-text-muted)',
          background: vote === 'down' ? '#dc262612' : 'transparent',
        }}
      >
        <ThumbsDown className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}

export function FeedbackCommentBox({ feedback }: { feedback: CardFeedbackState }) {
  if (!feedback.enabled || !feedback.vote || feedback.done) return null;
  return (
    <div
      className="flex items-center gap-2.5 px-5 py-2.5 border-b border-[var(--color-border)]/30"
      style={{ background: 'var(--color-bg-secondary)', opacity: 0.95 }}
    >
      <input
        type="text"
        value={feedback.comment}
        onChange={e => feedback.setComment(e.target.value)}
        onKeyDown={e => { if (e.key === 'Enter') feedback.submit(); }}
        placeholder="Add a comment (optional)…"
        maxLength={1000}
        className="flex-1 px-3 py-1.5 rounded-lg border text-sm outline-none transition-colors"
        style={{
          borderColor: 'var(--color-border)',
          background: 'var(--color-background)',
          color: 'var(--color-text-body)',
        }}
        onFocus={e => (e.currentTarget.style.borderColor = 'var(--color-accent-primary)')}
        onBlur={e => (e.currentTarget.style.borderColor = 'var(--color-border)')}
      />
      <button
        onClick={feedback.submit}
        disabled={feedback.submitting}
        className="flex-shrink-0 flex items-center gap-1.5 px-3.5 py-1.5 rounded-lg text-xs font-semibold text-white transition-all disabled:opacity-40 cursor-pointer"
        style={{ background: 'linear-gradient(135deg, var(--color-accent-primary) 0%, var(--color-accent-secondary) 100%)' }}
      >
        <Send className="h-3 w-3" />
        {feedback.submitting ? 'Sending…' : 'Send'}
      </button>
    </div>
  );
}
