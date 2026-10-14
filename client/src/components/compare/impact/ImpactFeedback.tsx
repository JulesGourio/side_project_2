import { createContext, useContext, useState } from 'react';
import { CheckCircle2, Send, ThumbsDown, ThumbsUp } from 'lucide-react';
import { toast } from 'sonner';
import { LS, lsGetJson, lsSet } from '../storage';
import type { ImpactDoc } from './types';

/** What a vote is attached to. */
export interface FeedbackTarget {
  impactRequestId: number | null;
  messageId: number | null;
  oldFileHash: string;
  newFileHash: string;
}

export const FeedbackContext = createContext<FeedbackTarget | null>(null);

type Vote = 'up' | 'down';

const VOTE_COLOR: Record<Vote, string> = { up: '#16a34a', down: '#dc2626' };
const MAX_REMEMBERED_VOTES = 300;

function readVotes(): Record<string, string> {
  return lsGetJson<Record<string, string>>(LS.impactVotes) ?? {};
}

function rememberVote(key: string, vote: string): void {
  const votes = { ...readVotes(), [key]: vote };
  const keys = Object.keys(votes);
  for (const k of keys.slice(0, Math.max(0, keys.length - MAX_REMEMBERED_VOTES))) delete votes[k];
  lsSet(LS.impactVotes, JSON.stringify(votes));
}

async function sendImpactFeedback(target: FeedbackTarget, body: Record<string, unknown>): Promise<void> {
  const res = await fetch('/api/compare/impact/feedback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      impact_request_id: target.impactRequestId,
      message_id: target.messageId,
      old_file_hash: target.oldFileHash || null,
      new_file_hash: target.newFileHash || null,
      ...body,
    }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
}

/** "Is this verdict right?" — one click, no comment: the per-document signal that measures the judge. */
export function VerdictVote({ doc }: { doc: ImpactDoc }) {
  const target = useContext(FeedbackContext);
  const key = `${target?.impactRequestId ?? 'none'}:${doc.ref}`;
  const [vote, setVote] = useState<string | null>(() => readVotes()[key] ?? null);
  if (!target || !doc.status || doc.status === 'error') return null;

  const cast = async (v: Vote) => {
    if (vote === v) return;
    setVote(v);
    rememberVote(key, v);
    try {
      await sendImpactFeedback(target, { vote: v, ref: doc.ref, verdict_shown: doc.status });
    } catch {
      toast.error('Your vote could not be saved.');
    }
  };
  const button = (v: Vote, label: string, Icon: typeof ThumbsUp) => (
    <button
      type="button"
      onClick={() => cast(v)}
      title={label}
      aria-label={label}
      aria-pressed={vote === v}
      className="p-1 rounded cursor-pointer transition-colors"
      style={{ color: vote === v ? VOTE_COLOR[v] : 'var(--color-text-muted)', background: vote === v ? `${VOTE_COLOR[v]}18` : 'transparent' }}
    >
      <Icon className="h-3.5 w-3.5" />
    </button>
  );
  return (
    <span className="ml-auto flex items-center gap-0.5">
      {button('up', 'This verdict is correct', ThumbsUp)}
      {button('down', 'This verdict is wrong', ThumbsDown)}
    </span>
  );
}

/** A vote on the whole result, with an optional comment. */
export function SearchFeedback() {
  const target = useContext(FeedbackContext);
  const key = `${target?.impactRequestId ?? 'none'}:`;
  const [vote, setVote] = useState<Vote | null>(null);
  const [comment, setComment] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [done, setDone] = useState(() => key in readVotes());
  if (!target) return null;

  if (done) {
    return (
      <div className="flex items-center gap-2 text-sm" style={{ color: 'var(--color-success)' }}>
        <CheckCircle2 className="h-4 w-4" />
        <span className="font-medium">Thank you for your feedback!</span>
      </div>
    );
  }
  const submit = async () => {
    if (!vote || submitting) return;
    setSubmitting(true);
    try {
      await sendImpactFeedback(target, { vote, comment: comment.trim() || null });
      rememberVote(key, vote);
      setDone(true);
    } catch {
      toast.error('Your feedback could not be saved.');
    } finally {
      setSubmitting(false);
    }
  };
  const button = (v: Vote, label: string, Icon: typeof ThumbsUp) => (
    <button
      type="button"
      onClick={() => setVote(v)}
      className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium border cursor-pointer transition-colors"
      style={{
        borderColor: vote === v ? VOTE_COLOR[v] : 'var(--color-border)',
        color: vote === v ? VOTE_COLOR[v] : 'var(--color-text-muted)',
        background: vote === v ? `${VOTE_COLOR[v]}10` : 'transparent',
      }}
    >
      <Icon className="h-3.5 w-3.5" />
      {label}
    </button>
  );
  return (
    <div className="pt-3 border-t border-[var(--color-border)]/40 space-y-2.5">
      <div className="flex flex-wrap items-center gap-2.5">
        <span className="text-xs font-medium text-[var(--color-text-heading)]">Was this impact search helpful?</span>
        {button('up', 'Helpful', ThumbsUp)}
        {button('down', 'Not helpful', ThumbsDown)}
      </div>
      {vote && (
        <div className="flex flex-wrap items-end gap-2">
          <textarea
            value={comment}
            onChange={e => setComment(e.target.value)}
            placeholder="Add a comment (optional) — a missing document, a wrong verdict…"
            rows={2}
            maxLength={1000}
            className="flex-1 min-w-[240px] px-3 py-2 rounded-lg border text-xs resize-none outline-none"
            style={{ borderColor: 'var(--color-border)', background: 'var(--color-background)', color: 'var(--color-text-primary)' }}
          />
          <button
            type="button"
            onClick={submit}
            disabled={submitting}
            className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-semibold text-white cursor-pointer disabled:opacity-50"
            style={{ background: 'var(--color-accent-primary)' }}
          >
            <Send className="h-3.5 w-3.5" />
            {submitting ? 'Sending…' : 'Submit'}
          </button>
        </div>
      )}
    </div>
  );
}
