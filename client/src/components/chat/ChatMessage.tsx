import { Bot, User } from 'lucide-react';
import { MarkdownRenderer, type CitationMap } from '@/components/shared/MarkdownRenderer';
import { ChatFeedback, ChatFeedbackReadOnly } from './ChatFeedback';
import { ChatSources } from './ChatSources';
import { DownloadMenu } from './DownloadMenu';
import type { Message } from './message';

const ACCENT_GRADIENT = 'linear-gradient(135deg, var(--color-accent-primary) 0%, var(--color-accent-secondary) 100%)';

function StreamingCursor() {
  return (
    <span
      className="inline-block w-0.5 h-4 ml-0.5 align-middle rounded-full"
      style={{ background: 'var(--color-accent-primary)', animation: 'blink 1s step-end infinite' }}
    />
  );
}

function ThinkingDots({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-1.5 py-1">
      <span className="text-sm" style={{ color: 'var(--color-text-muted)' }}>{label}</span>
      <span className="flex gap-1">
        {[0, 1, 2].map(i => (
          <span
            key={i}
            className="w-1.5 h-1.5 rounded-full"
            style={{ background: 'var(--color-accent-primary)', opacity: 0.7, animation: `bounce 1.2s ease-in-out ${i * 0.2}s infinite` }}
          />
        ))}
      </span>
    </div>
  );
}

/** Maps each citation number to its source, so inline ⟦n⟧ markers link to the document shown in the chips below. */
function citationsOf(message: Message): CitationMap {
  const map: CitationMap = {};
  for (const s of message.sources ?? []) {
    // Prose-only sources (no `n`) are never numbered inline in the text.
    if (s.n != null) map[s.n] = { url: s.url, title: s.title };
  }
  return map;
}

export function ChatMessage({ message, showFeedback, allMessages }: {
  message: Message;
  showFeedback: boolean;
  allMessages: Message[];
}) {
  if (message.role === 'user') {
    return (
      <div className="flex justify-end px-4 py-2">
        <div className="flex items-end gap-2 max-w-[75%]">
          <div
            className="px-4 py-2.5 rounded-2xl rounded-br-sm text-sm leading-relaxed"
            style={{ background: ACCENT_GRADIENT, color: '#fff', wordBreak: 'break-word' }}
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

  const done = !message.streaming && !message.error;

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
            style={{ background: 'var(--color-bg-secondary)', border: '1px solid var(--color-border)' }}
          >
            {message.error ? (
              <p className="text-sm italic" style={{ color: 'var(--color-text-muted)' }}>{message.content}</p>
            ) : message.streaming && !message.content ? (
              <ThinkingDots label={message.status === 'still_searching' ? 'Still searching' : 'Thinking'} />
            ) : (
              <div className="text-sm">
                <MarkdownRenderer content={message.content} citations={citationsOf(message)} />
                {message.streaming && <StreamingCursor />}
              </div>
            )}
          </div>

          {done && message.sources && message.sources.length > 0 && <ChatSources sources={message.sources} />}

          {done && (
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
