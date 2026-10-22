import { useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { PanelLeftClose, PanelLeftOpen, Share2 } from 'lucide-react';
import { WhatsNewButton, WhatsNewModal } from '@/components/layout/WhatsNewBanner';
import { ChatInput } from './ChatInput';
import { ChatMessage } from './ChatMessage';
import { ChatSidebar } from './ChatSidebar';
import type { Division } from './division';
import { useChatConversation } from './useChatConversation';
import { useChatSessions } from './useChatSessions';
import { WelcomeState } from './WelcomeState';

const CHAT_TITLE = 'Qualibot';
const CHAT_HEADER = 'Qualibot on Intraqual Documentation';

const HEADER_BUTTON = 'border transition-all cursor-pointer text-[var(--color-text-muted)] hover:text-[var(--color-text-primary)]';

function useDocsAsOf(): string | null {
  const [date, setDate] = useState<string | null>(null);
  useEffect(() => {
    fetch('/api/config/knowledge-base-date')
      .then(r => (r.ok ? r.json() : null))
      .then((data: { date: string | null } | null) => { if (data?.date) setDate(data.date); })
      .catch(() => {});
  }, []);
  return date;
}

export function ChatView() {
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [division, setDivision] = useState<Division>('ALL');
  const [showWhatsNew, setShowWhatsNew] = useState(false);
  const [searchParams, setSearchParams] = useSearchParams();
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const history = useChatSessions();
  const chat = useChatConversation(division, history.reload);
  const docsAsOf = useDocsAsOf();

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [chat.messages]);

  // Opened from a shared link's "Duplicate": /chat?session=<id>.
  useEffect(() => {
    const sid = searchParams.get('session');
    if (sid) {
      chat.select(sid);
      setSearchParams({}, { replace: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  const handleDelete = async (id: string) => {
    if ((await history.remove(id)) && id === chat.sessionId) chat.newChat();
  };

  const showSidebar = sidebarOpen && history.available;

  return (
    <div className="flex h-full overflow-hidden">
      {showSidebar && (
        <ChatSidebar
          sessions={history.sessions}
          currentSessionId={chat.sessionId}
          onSelectSession={chat.select}
          onNewChat={chat.newChat}
          onDeleteSession={handleDelete}
          loading={history.loading}
          division={division}
          onDivisionChange={setDivision}
        />
      )}

      <div className="flex flex-col flex-1 min-w-0">
        <div
          className="flex items-center gap-2 px-4 py-2 border-b flex-shrink-0"
          style={{ borderColor: 'var(--color-border)', background: 'var(--color-bg-primary)' }}
        >
          {history.available && (
            <button
              onClick={() => setSidebarOpen(o => !o)}
              className="w-7 h-7 rounded-lg flex items-center justify-center transition-all cursor-pointer text-[var(--color-text-muted)] hover:text-[var(--color-text-primary)]"
              title={sidebarOpen ? 'Hide sidebar' : 'Show sidebar'}
            >
              {sidebarOpen ? <PanelLeftClose className="h-4 w-4" /> : <PanelLeftOpen className="h-4 w-4" />}
            </button>
          )}
          <span className="text-sm font-semibold" style={{ color: 'var(--color-text-heading)', fontFamily: 'var(--font-heading)' }}>
            {CHAT_HEADER}
          </span>
          {docsAsOf && (
            <>
              <span style={{ color: 'var(--color-border)' }}>·</span>
              <span className="text-xs" style={{ color: 'var(--color-text-muted)' }}>
                Documents as of {new Date(docsAsOf + 'T12:00:00').toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' })} (only documents after 2016 are taken into account)
              </span>
            </>
          )}
          <div className="flex-1" />
          {chat.messages.length > 0 && (
            <button
              onClick={chat.share}
              disabled={chat.streaming}
              className={`flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs font-medium disabled:opacity-50 disabled:cursor-not-allowed border-[var(--color-border)] hover:border-[var(--color-accent-primary)] ${HEADER_BUTTON}`}
              title="Copy a read-only link to this conversation"
            >
              <Share2 className="h-3.5 w-3.5" />
              Share
            </button>
          )}
          <WhatsNewButton tabId="chat" open={showWhatsNew} onClick={() => setShowWhatsNew(o => !o)} />
        </div>

        {showWhatsNew && <WhatsNewModal tabId="chat" onClose={() => setShowWhatsNew(false)} />}

        <div className="flex-1 overflow-y-auto py-4">
          {chat.messages.length === 0 ? (
            <WelcomeState title={CHAT_TITLE} onPrompt={p => chat.send(p)} />
          ) : (
            <>
              {chat.messages.map(msg => (
                <ChatMessage
                  key={msg.id}
                  message={msg}
                  showFeedback={msg.role === 'assistant' && !msg.streaming && !msg.error}
                  allMessages={chat.messages}
                />
              ))}
              <div ref={messagesEndRef} />
            </>
          )}
        </div>

        <ChatInput
          value={chat.input}
          onChange={chat.setInput}
          onSend={() => chat.send()}
          onStop={chat.stop}
          disabled={chat.streaming}
          streaming={chat.streaming}
          placeholder={
            division === 'ALL'
              ? 'Ask a question about your documents…'
              : `Ask a question (scope: ${division} division)…`
          }
        />
      </div>
    </div>
  );
}
