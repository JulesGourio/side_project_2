import { useState } from 'react';
import { CheckCircle2, Send, ThumbsDown, ThumbsUp } from 'lucide-react';
import type { Feedback } from './message';

type Vote = Feedback['vote'];

const VOTE_COLOR: Record<Vote, string> = { up: '#16a34a', down: '#dc2626' };

export function ChatFeedback({ messageId, sessionId }: { messageId?: number; sessionId?: string }) {
  const [vote, setVote] = useState<Vote | null>(null);
  const [comment, setComment] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);

  const handleSubmit = async () => {
    if (!vote || submitting) return;
    setSubmitting(true);
    try {
      await fetch('/api/chat/feedback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          vote,
          comment: comment.trim() || null,
          message_id: messageId ?? null,
          session_id: sessionId ?? null,
        }),
      });
    } catch {
      // best-effort
    } finally {
      setSubmitting(false);
      setSubmitted(true);
    }
  };

  if (submitted) {
    return (
      <div className="flex items-center gap-2 mt-2 text-xs" style={{ color: 'var(--color-success)' }}>
        <CheckCircle2 className="h-3.5 w-3.5" />
        <span>Thanks for your feedback!</span>
      </div>
    );
  }

  const button = (kind: Vote, label: string, Icon: typeof ThumbsUp) => {
    const color = VOTE_COLOR[kind];
    const active = vote === kind;
    return (
      <button
        onClick={() => setVote(active ? null : kind)}
        className="flex items-center gap-1 px-2 py-1 rounded-lg text-xs font-medium border transition-all cursor-pointer"
        style={{
          borderColor: active ? color : 'var(--color-border)',
          color: active ? color : 'var(--color-text-muted)',
          background: active ? `${color}15` : 'transparent',
        }}
      >
        <Icon className="h-3 w-3" />
        {label}
      </button>
    );
  };

  return (
    <div className="mt-3 space-y-2">
      <div className="flex items-center gap-2">
        <span className="text-xs" style={{ color: 'var(--color-text-muted)' }}>Was this helpful?</span>
        {button('up', 'Yes', ThumbsUp)}
        {button('down', 'No', ThumbsDown)}
      </div>

      {vote && (
        <div className="flex gap-2">
          <input
            type="text"
            value={comment}
            onChange={e => setComment(e.target.value)}
            placeholder="Add a comment (optional)…"
            maxLength={500}
            className="flex-1 px-3 py-1.5 rounded-xl border text-xs outline-none transition-colors border-[var(--color-border)] focus:border-[var(--color-accent-primary)]"
            style={{ background: 'var(--color-bg-primary)', color: 'var(--color-text-primary)' }}
            onKeyDown={e => { if (e.key === 'Enter') handleSubmit(); }}
          />
          <button
            onClick={handleSubmit}
            disabled={submitting}
            className="flex items-center gap-1 px-3 py-1.5 rounded-xl text-xs font-semibold text-white transition-all disabled:opacity-50 cursor-pointer"
            style={{ background: 'linear-gradient(135deg, var(--color-accent-primary) 0%, var(--color-accent-secondary) 100%)' }}
          >
            <Send className="h-3 w-3" />
            {submitting ? '…' : 'Send'}
          </button>
        </div>
      )}
    </div>
  );
}

/** The owner's vote on a shared conversation; a viewer can never cast a new one. */
export function ChatFeedbackReadOnly({ feedback }: { feedback: Feedback }) {
  const isUp = feedback.vote === 'up';
  const accent = VOTE_COLOR[feedback.vote];
  const Icon = isUp ? ThumbsUp : ThumbsDown;

  return (
    <div className="mt-3 space-y-1.5">
      <div
        className="inline-flex items-center gap-1 px-2 py-1 rounded-lg text-xs font-medium border"
        style={{ borderColor: accent, color: accent, background: `${accent}15` }}
      >
        <Icon className="h-3 w-3" />
        {isUp ? 'Marked helpful' : 'Marked not helpful'}
      </div>
      {feedback.comment && (
        <p className="text-xs italic" style={{ color: 'var(--color-text-muted)' }}>“{feedback.comment}”</p>
      )}
    </div>
  );
}
