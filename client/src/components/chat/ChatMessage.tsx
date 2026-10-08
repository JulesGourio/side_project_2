import { useEffect, useRef, useState } from 'react';
import { ThumbsUp, ThumbsDown, CheckCircle2, Send, User, Bot, FileText, Download } from 'lucide-react';
import { MarkdownRenderer, type CitationMap } from '@/components/shared/MarkdownRenderer';
import { Flag } from '@/components/shared/Flag';
import { downloadAsPdf } from '@/lib/downloadAsPdf';

export interface Source {
  title: string;
  url?: string;
  // 1-based inline-citation number. Present only for sources actually cited
  // inline (⟦n⟧ in the text); prose-only sources re-surfaced from the catalog
  // have no number and render as a plain REF chip.
  n?: number;
}

export interface Feedback {
  vote: 'up' | 'down';
  comment?: string | null;
}

export interface Message {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  streaming?: boolean;
  dbId?: number;
  sessionId?: string;
  error?: boolean;
  sources?: Source[];
  // Only populated on the shared read-only view — the owner's existing vote,
  // never editable by a viewer (see SharedChatPage).
  feedback?: Feedback | null;
}

// --- Download utilities ---

function triggerDownload(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

// Inline citation markers (⟦n⟧) are for on-screen rendering only — turn them
// into plain [n] for downloads / printing.
function stripCiteMarkers(content: string): string {
  return content.replace(/⟦(\d+)⟧/g, '[$1]');
}

function downloadAsMarkdown(content: string, filename: string) {
  triggerDownload(new Blob([content], { type: 'text/markdown' }), filename);
}



function buildSessionMarkdown(messages: Message[]): string {
  const date = new Date().toLocaleDateString('en-GB');
  const parts = [`# QualiBOT — Session ${date}\n`];
  for (const msg of messages) {
    if (msg.role === 'user') {
      parts.push(`## Question\n\n${msg.content}`);
    } else if (!msg.error && msg.content) {
      parts.push(`## Response\n\n${stripCiteMarkers(msg.content)}`);
    }
  }
  return parts.join('\n\n---\n\n');
}


// --- Download menu ---

interface DownloadMenuProps {
  message: Message;
  allMessages: Message[];
}

function DownloadMenu({ message, allMessages }: DownloadMenuProps) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [open]);

  const slug = new Date().toISOString().slice(0, 10);

  const options = [
    {
      label: 'Response — Markdown',
      action: () => downloadAsMarkdown(stripCiteMarkers(message.content), `qualibot-response-${slug}.md`),
    },
    {
      label: 'Response — PDF',
      action: () => downloadAsPdf('Response', stripCiteMarkers(message.content), `qualibot-response-${slug}.pdf`),
    },
    {
      label: 'Full session — Markdown',
      action: () => downloadAsMarkdown(buildSessionMarkdown(allMessages), `qualibot-session-${slug}.md`),
    },
    {
      label: 'Full session — PDF',
      action: () => downloadAsPdf('QualiBOT Session', buildSessionMarkdown(allMessages), `qualibot-session-${slug}.pdf`),
    },
  ];

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen(o => !o)}
        className="flex items-center gap-1 px-2 py-1 rounded-lg text-xs border transition-all cursor-pointer"
        title="Download"
        style={{
          borderColor: 'var(--color-border)',
          color: 'var(--color-text-muted)',
          background: 'transparent',
        }}
        onMouseEnter={e => {
          (e.currentTarget as HTMLButtonElement).style.color = 'var(--color-text-primary)';
          (e.currentTarget as HTMLButtonElement).style.borderColor = 'var(--color-accent-primary)';
        }}
        onMouseLeave={e => {
          (e.currentTarget as HTMLButtonElement).style.color = 'var(--color-text-muted)';
          (e.currentTarget as HTMLButtonElement).style.borderColor = 'var(--color-border)';
        }}
      >
        <Download className="h-3.5 w-3.5" />
        <span>Télécharger</span>
      </button>

      {open && (
        <div
          className="absolute bottom-full right-0 mb-1 rounded-xl border shadow-xl z-50 overflow-hidden min-w-[220px]"
          style={{
            background: 'var(--color-bg-secondary)',
            borderColor: 'var(--color-border)',
          }}
        >
          {options.map(opt => (
            <button
              key={opt.label}
              onClick={() => { opt.action(); setOpen(false); }}
              className="w-full text-left px-4 py-2.5 text-xs transition-colors cursor-pointer"
              style={{ color: 'var(--color-text-primary)' }}
              onMouseEnter={e => (e.currentTarget.style.background = 'var(--color-bg-tertiary)')}
              onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
            >
              {opt.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// --- Sources ---

// Mirrors doc_catalog.py's _canon(): same document, different intraqual
// site/language suffix (GO-1316_FR / GO-1316_GB / PRLAT-529 / PRLAT-529_GB /
// PRLAT536_FR...). Used only to group chips for display — matching/lookup
// already happened server-side.
const LANG_SUFFIX_RE = /[-_. ]+(FR|EN|GB|MX|BG|CZ|BR|ES)$/i;

function langCode(title: string): string | null {
  const m = title.match(LANG_SUFFIX_RE);
  return m ? m[1].toUpperCase() : null;
}

function canonicalDocKey(title: string): string {
  return title.toUpperCase().replace(LANG_SUFFIX_RE, '').replace(/[^A-Z0-9]/g, '');
}

// Fixed display order — NOT a claim about which variant is "the" REF.
// Intraqual has no reliable default language (some documents' undecorated
// REF is the French original, others' is the English one), so the group
// label is the shared document code (suffix stripped), not one language's
// title — see SourceChipGroup.
const LANG_ORDER = ['FR', 'EN', 'GB', 'ES', 'MX', 'BG', 'CZ', 'BR'];

// The inline-cited variant (if any) leads the row — that replaces the old
// numbered badge as the "which one did the model actually cite" signal.
function sortVariants(variants: Source[]): Source[] {
  return [...variants].sort((a, b) => {
    if ((a.n != null) !== (b.n != null)) return a.n != null ? -1 : 1;
    const ca = langCode(a.title || '');
    const cb = langCode(b.title || '');
    if (!ca || !cb) return (ca ? 0 : 1) - (cb ? 0 : 1);
    return LANG_ORDER.indexOf(ca) - LANG_ORDER.indexOf(cb);
  });
}

const chipBase = 'group inline-flex items-center gap-1.5 max-w-[260px] py-1 rounded-full border text-xs transition-all';
const chipStyle = { borderColor: 'var(--color-border)', background: 'var(--color-bg-secondary)' };

function chipNumberBadge(n: number) {
  return (
    <span
      className="flex items-center justify-center h-4 w-4 rounded-full text-[10px] font-semibold flex-shrink-0"
      style={{ background: 'var(--color-accent-primary)', color: '#fff' }}
    >
      {n}
    </span>
  );
}

// A single, un-grouped source: exactly the plain pill this always used to be.
function SourceChip({ src, fallbackLabel }: { src: Source; fallbackLabel: string }) {
  const label = src.title || src.url || fallbackLabel;
  const pad = src.n != null ? 'pl-1 pr-2.5' : 'px-2.5';
  const text = (
    <span className="truncate font-medium" style={{ color: 'var(--color-text-primary)' }}>
      {label}
    </span>
  );

  return src.url ? (
    <a
      href={src.url}
      target="_blank"
      rel="noopener noreferrer"
      title={src.url && label !== src.url ? `${label}\n${src.url}` : label}
      className={`${chipBase} ${pad} cursor-pointer`}
      style={chipStyle}
      onMouseEnter={e => (e.currentTarget.style.borderColor = 'var(--color-accent-primary)')}
      onMouseLeave={e => (e.currentTarget.style.borderColor = 'var(--color-border)')}
    >
      {src.n != null && chipNumberBadge(src.n)}
      {text}
    </a>
  ) : (
    <span title={label} className={`${chipBase} ${pad}`} style={chipStyle}>
      {src.n != null && chipNumberBadge(src.n)}
      {text}
    </span>
  );
}

// A document with 2+ site/language variants: one chip showing the shared
// document code (the REF with its language suffix stripped — the same code
// for every variant, so it doesn't privilege whichever language happens to
// be the "bare" one) plus a small flag per available language. Clicking the
// code opens the bare/undecorated variant if there is one; each flag opens
// its own language's document. Full REFs are in the chip's tooltip.
function SourceChipGroup({ variants }: { variants: Source[] }) {
  const bare = variants.find(v => !langCode(v.title || ''));
  const label = (variants[0]?.title || variants[0]?.url || 'Source').replace(LANG_SUFFIX_RE, '');
  const flagVariants = sortVariants(variants).filter(v => v !== bare);
  const tooltip = variants.map(v => v.title).filter(Boolean).join('  ·  ');

  return (
    <span className={`${chipBase} px-2.5`} style={chipStyle} title={tooltip}>
      {bare?.url ? (
        <a
          href={bare.url}
          target="_blank"
          rel="noopener noreferrer"
          className="truncate font-medium hover:underline"
          style={{ color: 'var(--color-text-primary)' }}
        >
          {label}
        </a>
      ) : (
        <span className="truncate font-medium opacity-40" style={{ color: 'var(--color-text-primary)' }}>
          {label}
        </span>
      )}
      <span className="flex items-center gap-1 pl-1.5 ml-0.5 border-l flex-shrink-0" style={{ borderColor: 'var(--color-border)' }}>
        {flagVariants.map((v, j) => {
          const code = langCode(v.title || '');
          if (!code) return null;
          return v.url ? (
            <a key={j} href={v.url} target="_blank" rel="noopener noreferrer" title={v.title} className="transition-transform hover:scale-125">
              <Flag code={code} />
            </a>
          ) : (
            <span key={j} title={v.title} className="opacity-40">
              <Flag code={code} />
            </span>
          );
        })}
      </span>
    </span>
  );
}

// Clickable source chips — each consulted document is a small pill showing its
// REF (reference code). Sources cited inline carry a small number badge that
// matches their [n] marker in the answer; prose-only sources re-surfaced from
// the catalog have no number and show just the REF. Chips with a URL open it in
// a new tab; chips without one are a plain badge (full ref on hover). Documents
// with several intraqual site/language variants (see canonicalDocKey) collapse
// into a single chip with per-language badges instead of one chip each.
function ChatSources({ sources }: { sources: Source[] }) {
  if (!sources.length) return null;

  const groups: Source[][] = [];
  const indexByKey = new Map<string, number>();
  for (const src of sources) {
    const key = canonicalDocKey(src.title || src.url || '');
    const idx = indexByKey.get(key);
    if (idx === undefined) {
      indexByKey.set(key, groups.length);
      groups.push([src]);
    } else {
      groups[idx].push(src);
    }
  }

  return (
    <div className="mt-2 flex flex-wrap items-center gap-1.5">
      <span
        className="inline-flex items-center gap-1 text-xs font-medium mr-0.5"
        style={{ color: 'var(--color-text-muted)' }}
      >
        <FileText className="h-3.5 w-3.5 flex-shrink-0" />
        Sources&nbsp;:
      </span>

      {groups.map((variants, i) =>
        variants.length > 1 ? (
          <SourceChipGroup key={i} variants={variants} />
        ) : (
          <SourceChip key={i} src={variants[0]} fallbackLabel={`Source ${i + 1}`} />
        )
      )}
    </div>
  );
}

// --- Feedback ---

interface FeedbackProps {
  messageId?: number;
  sessionId?: string;
}

function ChatFeedback({ messageId, sessionId }: FeedbackProps) {
  const [vote, setVote] = useState<'up' | 'down' | null>(null);
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

  const accentUp = '#16a34a';
  const accentDown = '#dc2626';

  return (
    <div className="mt-3 space-y-2">
      <div className="flex items-center gap-2">
        <span className="text-xs" style={{ color: 'var(--color-text-muted)' }}>Was this helpful?</span>
        <button
          onClick={() => setVote(vote === 'up' ? null : 'up')}
          className="flex items-center gap-1 px-2 py-1 rounded-lg text-xs font-medium border transition-all cursor-pointer"
          style={{
            borderColor: vote === 'up' ? accentUp : 'var(--color-border)',
            color: vote === 'up' ? accentUp : 'var(--color-text-muted)',
            background: vote === 'up' ? `${accentUp}15` : 'transparent',
          }}
        >
          <ThumbsUp className="h-3 w-3" />
          Yes
        </button>
        <button
          onClick={() => setVote(vote === 'down' ? null : 'down')}
          className="flex items-center gap-1 px-2 py-1 rounded-lg text-xs font-medium border transition-all cursor-pointer"
          style={{
            borderColor: vote === 'down' ? accentDown : 'var(--color-border)',
            color: vote === 'down' ? accentDown : 'var(--color-text-muted)',
            background: vote === 'down' ? `${accentDown}15` : 'transparent',
          }}
        >
          <ThumbsDown className="h-3 w-3" />
          No
        </button>
      </div>

      {vote && (
        <div className="flex gap-2">
          <input
            type="text"
            value={comment}
            onChange={e => setComment(e.target.value)}
            placeholder="Add a comment (optional)…"
            maxLength={500}
            className="flex-1 px-3 py-1.5 rounded-xl border text-xs outline-none transition-colors"
            style={{
              borderColor: 'var(--color-border)',
              background: 'var(--color-bg-primary)',
              color: 'var(--color-text-primary)',
            }}
            onFocus={e => (e.currentTarget.style.borderColor = 'var(--color-accent-primary)')}
            onBlur={e => (e.currentTarget.style.borderColor = 'var(--color-border)')}
            onKeyDown={e => { if (e.key === 'Enter') handleSubmit(); }}
          />
          <button
            onClick={handleSubmit}
            disabled={submitting}
            className="flex items-center gap-1 px-3 py-1.5 rounded-xl text-xs font-semibold text-white transition-all disabled:opacity-50 cursor-pointer"
            style={{
              background: 'linear-gradient(135deg, var(--color-accent-primary) 0%, var(--color-accent-secondary) 100%)',
            }}
          >
            <Send className="h-3 w-3" />
            {submitting ? '…' : 'Send'}
          </button>
        </div>
      )}
    </div>
  );
}

function ChatFeedbackReadOnly({ feedback }: { feedback: Feedback }) {
  const isUp = feedback.vote === 'up';
  const accent = isUp ? '#16a34a' : '#dc2626';
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
        <p className="text-xs italic" style={{ color: 'var(--color-text-muted)' }}>
          “{feedback.comment}”
        </p>
      )}
    </div>
  );
}

// --- Streaming cursor ---

function StreamingCursor() {
  return (
    <span
      className="inline-block w-0.5 h-4 ml-0.5 align-middle rounded-full"
      style={{
        background: 'var(--color-accent-primary)',
        animation: 'blink 1s step-end infinite',
      }}
    />
  );
}

// --- Message bubble ---

interface ChatMessageProps {
  message: Message;
  showFeedback: boolean;
  allMessages: Message[];
}

export function ChatMessage({ message, showFeedback, allMessages }: ChatMessageProps) {
  const isUser = message.role === 'user';

  // Map each citation number to its source, so inline ⟦n⟧ markers link to the
  // same document shown in the numbered chips below. Keyed by the source's own
  // citation number `n` — prose-only sources (no `n`) are intentionally excluded
  // so they never get numbered inline in the text.
  const citationMap: CitationMap = {};
  (message.sources ?? []).forEach((s) => {
    if (s.n != null) citationMap[s.n] = { url: s.url, title: s.title };
  });

  if (isUser) {
    return (
      <div className="flex justify-end px-4 py-2">
        <div className="flex items-end gap-2 max-w-[75%]">
          <div
            className="px-4 py-2.5 rounded-2xl rounded-br-sm text-sm leading-relaxed"
            style={{
              background: 'linear-gradient(135deg, var(--color-accent-primary) 0%, var(--color-accent-secondary) 100%)',
              color: '#fff',
              wordBreak: 'break-word',
            }}
          >
            {message.content}
          </div>
          <div
            className="w-7 h-7 rounded-full flex-shrink-0 flex items-center justify-center mb-0.5"
            style={{ background: 'var(--color-accent-primary)', color: '#fff' }}
          >
            <User className="h-4 w-4" />
          </div>
        </div>
      </div>
    );
  }

  // Assistant message
  return (
    <div className="flex justify-start px-4 py-2">
      <div className="flex items-start gap-2 max-w-[85%]">
        <div
          className="w-7 h-7 rounded-full flex-shrink-0 flex items-center justify-center mt-0.5"
          style={{ background: 'var(--color-bg-tertiary)', color: 'var(--color-accent-primary)' }}
        >
          <Bot className="h-4 w-4" />
        </div>
        <div className="flex-1 min-w-0">
          <div
            className="px-4 py-3 rounded-2xl rounded-tl-sm"
            style={{
              background: 'var(--color-bg-secondary)',
              border: '1px solid var(--color-border)',
            }}
          >
            {message.error ? (
              <p className="text-sm" style={{ color: 'var(--color-error)' }}>
                {message.content}
              </p>
            ) : message.streaming && !message.content ? (
              <div className="flex items-center gap-1.5 py-1">
                <span className="text-sm" style={{ color: 'var(--color-text-muted)' }}>Thinking</span>
                <span className="flex gap-1">
                  {[0, 1, 2].map(i => (
                    <span
                      key={i}
                      className="w-1.5 h-1.5 rounded-full"
                      style={{
                        background: 'var(--color-accent-primary)',
                        opacity: 0.7,
                        animation: `bounce 1.2s ease-in-out ${i * 0.2}s infinite`,
                      }}
                    />
                  ))}
                </span>
              </div>
            ) : (
              <div className="text-sm">
                <MarkdownRenderer content={message.content} citations={citationMap} />
                {message.streaming && <StreamingCursor />}
              </div>
            )}
          </div>

          {/* Sources — shown once streaming completes */}
          {!message.streaming && !message.error && message.sources && message.sources.length > 0 && (
            <ChatSources sources={message.sources} />
          )}

          {/* Feedback + Download — only after streaming completes */}
          {!message.streaming && !message.error && (
            <div className="flex items-start justify-between gap-4 mt-1">
              <div className="flex-1">
                {showFeedback ? (
                  <ChatFeedback messageId={message.dbId} sessionId={message.sessionId} />
                ) : (
                  message.feedback && <ChatFeedbackReadOnly feedback={message.feedback} />
                )}
              </div>
              <DownloadMenu message={message} allMessages={allMessages} />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
