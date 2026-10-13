import { useEffect, useState } from 'react';
import { CheckCircle2, Send, ThumbsDown, ThumbsUp } from 'lucide-react';
import type { FeedbackProps } from './types';
import { isFeedbackSubmitted, markFeedbackSubmitted } from './storage';

type Vote = 'up' | 'down';

/** Thumbs-up / thumbs-down vote with an optional comment on a result card, sent once per comparison. */
export function useCardFeedback(feedbackProps?: FeedbackProps) {
  const [vote, setVote] = useState<Vote | null>(null);
  const [comment, setComment] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [done, setDone] = useState(false);
  const submissionKey = feedbackProps?.submissionKey;

  useEffect(() => {
    if (!submissionKey) return;
    if (isFeedbackSubmitted(submissionKey)) {
      setDone(true);
    } else {
      setDone(false);
      setVote(null);
      setComment('');
    }
  }, [submissionKey]);

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

const VOTE_COLOR: Record<Vote, string> = { up: '#16a34a', down: '#dc2626' };

function ThumbButton({ kind, active, title, onClick }: { kind: Vote; active: boolean; title: string; onClick: () => void }) {
  const Icon = kind === 'up' ? ThumbsUp : ThumbsDown;
  const color = VOTE_COLOR[kind];
  return (
    <button
      onClick={onClick}
      title={title}
      className="w-7 h-7 rounded-lg flex items-center justify-center border transition-all cursor-pointer"
      style={{
        borderColor: active ? color : 'var(--color-border)',
        color: active ? color : 'var(--color-text-muted)',
        background: active ? `${color}12` : 'transparent',
      }}
    >
      <Icon className="h-3.5 w-3.5" />
    </button>
  );
}

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
      <ThumbButton kind="up" title="Helpful" active={vote === 'up'} onClick={() => setVote(v => (v === 'up' ? null : 'up'))} />
      <ThumbButton kind="down" title="Not helpful" active={vote === 'down'} onClick={() => setVote(v => (v === 'down' ? null : 'down'))} />
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
        className="flex-1 px-3 py-1.5 rounded-lg border text-sm outline-none transition-colors border-[var(--color-border)] focus:border-[var(--color-accent-primary)]"
        style={{ background: 'var(--color-background)', color: 'var(--color-text-body)' }}
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
