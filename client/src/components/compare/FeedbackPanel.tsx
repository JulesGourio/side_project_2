import { useState } from 'react';
import { ThumbsUp, ThumbsDown, CheckCircle2, Send } from 'lucide-react';

interface FeedbackPanelProps {
  messageId: number | null;
  oldFilename: string;
  newFilename: string;
  oldFileHash: string;
  newFileHash: string;
  sessionPath: string;
}

type Vote = 'up' | 'down';

export function FeedbackPanel({
  messageId,
  oldFilename,
  newFilename,
  oldFileHash,
  newFileHash,
  sessionPath,
}: FeedbackPanelProps) {
  const [vote, setVote] = useState<Vote | null>(null);
  const [comment, setComment] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);

  const handleSubmit = async () => {
    if (!vote || submitting) return;
    setSubmitting(true);
    try {
      await fetch('/api/feedback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          vote,
          comment: comment.trim() || null,
          message_id: messageId ?? null,
          old_filename: oldFilename || null,
          new_filename: newFilename || null,
          old_file_hash: oldFileHash || null,
          new_file_hash: newFileHash || null,
          session_path: sessionPath || null,
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
      <div className="flex items-center gap-2.5 px-5 py-3.5 rounded-2xl border border-[var(--color-success)]/25 bg-[var(--color-success)]/5 text-sm text-[var(--color-success)]">
        <CheckCircle2 className="h-4 w-4 flex-shrink-0" />
        <span className="font-medium">Thank you for your feedback!</span>
      </div>
    );
  }

  const accentUp   = '#16a34a';
  const accentDown = '#dc2626';

  return (
    <div className="rounded-2xl border border-[var(--color-border)]/40 bg-[var(--color-bg-secondary)]/30 px-5 py-4 space-y-4">
      <p className="text-sm font-medium text-[var(--color-text-heading)]">
        Was this analysis helpful?
      </p>

      <div className="flex items-center gap-3">
        <button
          onClick={() => !submitted && setVote('up')}
          className="flex items-center gap-2 px-4 py-2 rounded-xl text-sm font-medium border transition-all cursor-pointer"
          style={{
            borderColor: vote === 'up' ? accentUp : 'var(--color-border)',
            color: vote === 'up' ? accentUp : 'var(--color-text-muted)',
            background: vote === 'up' ? `${accentUp}10` : 'transparent',
          }}
        >
          <ThumbsUp className="h-4 w-4" />
          Helpful
        </button>

        <button
          onClick={() => !submitted && setVote('down')}
          className="flex items-center gap-2 px-4 py-2 rounded-xl text-sm font-medium border transition-all cursor-pointer"
          style={{
            borderColor: vote === 'down' ? accentDown : 'var(--color-border)',
            color: vote === 'down' ? accentDown : 'var(--color-text-muted)',
            background: vote === 'down' ? `${accentDown}10` : 'transparent',
          }}
        >
          <ThumbsDown className="h-4 w-4" />
          Not helpful
        </button>
      </div>

      {vote && (
        <div className="space-y-3">
          <textarea
            value={comment}
            onChange={e => setComment(e.target.value)}
            placeholder="Add a comment (optional)…"
            rows={3}
            maxLength={1000}
            className="w-full px-3.5 py-2.5 rounded-xl border text-sm resize-none outline-none transition-colors"
            style={{
              borderColor: 'var(--color-border)',
              background: 'var(--color-background)',
              color: 'var(--color-text-body)',
            }}
            onFocus={e => (e.currentTarget.style.borderColor = 'var(--color-accent-primary)')}
            onBlur={e => (e.currentTarget.style.borderColor = 'var(--color-border)')}
          />
          <div className="flex justify-end">
            <button
              onClick={handleSubmit}
              disabled={submitting}
              className="flex items-center gap-2 px-4 py-2 rounded-xl text-sm font-semibold text-white transition-all disabled:opacity-50 cursor-pointer"
              style={{
                background: 'linear-gradient(135deg, var(--color-accent-primary) 0%, var(--color-accent-secondary) 100%)',
              }}
            >
              <Send className="h-3.5 w-3.5" />
              {submitting ? 'Sending…' : 'Submit'}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
