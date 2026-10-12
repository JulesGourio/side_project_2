import { useCallback, useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { MessageSquare, PanelLeftClose, PanelLeftOpen, Share2 } from 'lucide-react';
import { toast } from 'sonner';
import { ChatInput } from './ChatInput';
import { ChatMessage, type Message, type Source } from './ChatMessage';
import { ChatSidebar, type ChatSession } from './ChatSidebar';
import { type Division, stripDivision } from './division';
import { WhatsNewButton, WhatsNewModal } from '@/components/layout/WhatsNewBanner';

// --- WebSocket streaming helper ---

const WS_PATH = '/api/chat/ws';
const CHAT_TITLE = 'Qualibot';
const CHAT_HEADER = 'Qualibot on Intraqual Documentation';

interface StreamCallbacks {
  onDelta: (text: string) => void;
  onDone: (data: { session_id: string; message_id?: number; content?: string; sources?: Source[] }) => void;
  onError: (err: string) => void;
}

// The only failure text a user sees (same as the server's TIRED_MESSAGE): the cause is logged server-side.
export const TIRED_MESSAGE = 'Qualibot is a bit tired right now. Please wait a moment and try again.';
// A socket that fails before the question was sent is opened again this many times.
const CONNECT_RETRIES = 2;

function streamChat(
  messages: { role: string; content: string }[],
  sessionId: string,
  division: Division,
  callbacks: StreamCallbacks,
  signal: AbortSignal,
  attempt = 0,
): Promise<void> {
  return new Promise((resolve) => {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${proto}//${location.host}${WS_PATH}`);
    let sent = false;
    // Nothing reached the server yet: open a new socket instead of failing the turn.
    const retryConnect = () => {
      settled = true;
      setTimeout(() => {
        if (signal.aborted) { resolve(); return; }
        streamChat(messages, sessionId, division, callbacks, signal, attempt + 1).then(resolve);
      }, 1000 * (attempt + 1));
    };

    // A turn always ends with exactly one outcome: done, error, or user abort.
    // A socket that closes without one must not leave the message on "Thinking".
    let settled = false;
    const finish = () => { settled = true; resolve(); };
    const cleanup = () => { try { ws.close(); } catch { /* ignore */ } };
    signal.addEventListener('abort', () => { settled = true; cleanup(); resolve(); });

    ws.onopen = () => {
      ws.send(JSON.stringify({ messages, session_id: sessionId, division }));
      sent = true;
    };

    ws.onmessage = (event) => {
      try {
        const parsed = JSON.parse(event.data as string);
        const type = parsed.type as string;
        if (type === 'delta') {
          callbacks.onDelta(parsed.delta ?? '');
        } else if (type === 'done') {
          callbacks.onDone({ session_id: parsed.session_id, message_id: parsed.message_id, content: parsed.content, sources: parsed.sources ?? [] });
          finish();
        } else if (type === 'error') {
          callbacks.onError(parsed.error || TIRED_MESSAGE);
          finish();
        }
      } catch {
        // ignore malformed
      }
    };

    ws.onerror = () => {
      if (settled) return;
      if (!sent && attempt < CONNECT_RETRIES) { retryConnect(); return; }
      callbacks.onError(TIRED_MESSAGE);
      finish();
    };

    ws.onclose = () => {
      if (settled) return;
      if (!sent && attempt < CONNECT_RETRIES) { retryConnect(); return; }
      callbacks.onError(TIRED_MESSAGE);
      finish();
    };
  });
}

// --- generateId helper ---

function genId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
}

function genSessionId(): string {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) return crypto.randomUUID();
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

// --- Welcome state ---

const SUGGESTED_PROMPTS = [
  'What documents reference the NDT/NDI qualification requirements?',
  'Which procedures must be updated when a supplier changes their process?',
  'List the key quality standards applicable to composite part manufacturing.',
  'What is the approval process for deviations from engineering specifications?',
];

function WelcomeState({ title, onPrompt }: { title: string; onPrompt: (p: string) => void }) {
  return (
    <div className="flex flex-col items-center justify-center h-full gap-8 px-6">
      {/* En-tête recentré, plus grand et parfaitement aligné avec la grille */}
      <div className="text-center space-y-4 max-w-lg">
        <div
          className="w-16 h-16 rounded-2xl flex items-center justify-center mx-auto shadow-sm"
          style={{ background: 'var(--color-accent-primary)20' }}
        >
          <MessageSquare className="h-8 w-8" style={{ color: 'var(--color-accent-primary)' }} />
        </div>
        <h2
          className="text-3xl font-bold tracking-tight"
          style={{ color: 'var(--color-text-heading)', fontFamily: 'var(--font-heading)' }}
        >
          {title}
        </h2>
        <p className="text-base" style={{ color: 'var(--color-text-muted)' }}>
          Ask questions about your document knowledge base (Intraqual). The assistant can retrieve relevant
          information and identify impacted documents.
        </p>
      </div>

      {/* Grille des prompts suggérés */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5 w-full max-w-lg mt-2">
        {SUGGESTED_PROMPTS.map((prompt, i) => (
          <button
            key={i}
            onClick={() => onPrompt(prompt)}
            className="text-left px-4 py-3 rounded-xl border text-sm leading-snug transition-all cursor-pointer"
            style={{
              borderColor: 'var(--color-border)',
              color: 'var(--color-text-primary)',
              background: 'var(--color-bg-secondary)',
            }}
            onMouseEnter={e => {
              (e.currentTarget as HTMLButtonElement).style.borderColor = 'var(--color-accent-primary)';
              (e.currentTarget as HTMLButtonElement).style.background = 'var(--color-bg-tertiary)';
            }}
            onMouseLeave={e => {
              (e.currentTarget as HTMLButtonElement).style.borderColor = 'var(--color-border)';
              (e.currentTarget as HTMLButtonElement).style.background = 'var(--color-bg-secondary)';
            }}
          >
            {prompt}
          </button>
        ))}
      </div>

      {/* Disclaimer (légèrement ajusté en taille comme demandé) */}
      <div className="max-w-xl mt-6 px-4 text-center">
        <p className="text-xs leading-relaxed" style={{ color: 'var(--color-text-muted)', opacity: 0.85 }}>
          <strong>Disclaimer:</strong> To help improve QualiBot and ensure its proper operation, user prompts may be logged and reviewed by authorized members of the Data & AI team. These logs are used exclusively for monitoring, troubleshooting, anomaly detection, and service improvement.
        </p>
      </div>
    </div>
  );
}

// --- Main ChatView ---

export function ChatView() {
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [sessionsAvailable, setSessionsAvailable] = useState(false);
  const [sessionsLoading, setSessionsLoading] = useState(true);

  const [currentSessionId, setCurrentSessionId] = useState<string>(() => genSessionId());
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [division, setDivision] = useState<Division>('ALL');
  const [docsAsOf, setDocsAsOf] = useState<string | null>(null);
  const [showWhatsNew, setShowWhatsNew] = useState(false);

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const sessionPersistedRef = useRef(false);
  const selectSeqRef = useRef(0);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const loadSessions = useCallback(async () => {
    try {
      const res = await fetch('/api/chat/sessions');
      if (!res.ok) return;
      const data = await res.json();
      setSessions(data.sessions ?? []);
      setSessionsAvailable(data.available ?? false);
    } catch {
      // DB not available — hide sidebar history
    } finally {
      setSessionsLoading(false);
    }
  }, []);

  useEffect(() => {
    loadSessions();
  }, [loadSessions]);

  const [searchParams, setSearchParams] = useSearchParams();

  useEffect(() => {
    fetch('/api/config/knowledge-base-date')
      .then(r => r.ok ? r.json() : null)
      .then((data: { date: string | null } | null) => { if (data?.date) setDocsAsOf(data.date); })
      .catch(() => {});
  }, []);

  const handleSelectSession = useCallback(async (id: string) => {
    if (streaming) return;
    selectSeqRef.current += 1;
    const seq = selectSeqRef.current;
    try {
      const res = await fetch(`/api/chat/sessions/${id}`);
      if (!res.ok) { toast.error('Failed to load conversation'); return; }
      const data = await res.json();
      if (seq !== selectSeqRef.current) return;
      setCurrentSessionId(id);
      sessionPersistedRef.current = true;
      setMessages(
        (data.messages ?? []).map((m: { id: number; role: string; content: string; sources?: Source[] }) => ({
          id: String(m.id),
          role: m.role as 'user' | 'assistant',
          content: m.role === 'user' ? stripDivision(m.content) : m.content,
          dbId: m.role === 'assistant' ? m.id : undefined,
          sessionId: id,
          sources: m.sources,
        })),
      );
    } catch {
      toast.error('Failed to load conversation');
    }
  }, [streaming]);

  useEffect(() => {
    const sid = searchParams.get('session');
    if (sid) {
      handleSelectSession(sid);
      setSearchParams({}, { replace: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  const handleNewChat = useCallback(() => {
    if (streaming) { abortRef.current?.abort(); }
    setCurrentSessionId(genSessionId());
    sessionPersistedRef.current = false;
    setMessages([]);
    setInput('');
    setStreaming(false);
  }, [streaming]);

  const handleDeleteSession = useCallback(async (id: string) => {
    try {
      await fetch(`/api/chat/sessions/${id}`, { method: 'DELETE' });
      setSessions(prev => prev.filter(s => s.id !== id));
      if (id === currentSessionId) handleNewChat();
    } catch {
      toast.error('Failed to delete conversation');
    }
  }, [currentSessionId, handleNewChat]);

  const handleShare = useCallback(async () => {
    if (!sessionPersistedRef.current) return;
    try {
      const res = await fetch(`/api/chat/sessions/${currentSessionId}/share`, { method: 'POST' });
      if (!res.ok) { toast.error('Failed to create the share link'); return; }
      const data = await res.json();
      const url = `${location.origin}/chat/shared/${data.share_token}`;
      await navigator.clipboard.writeText(url);
      toast.success('Share link copied to clipboard');
    } catch {
      toast.error('Failed to create the share link');
    }
  }, [currentSessionId]);

  const handleStop = useCallback(() => {
    abortRef.current?.abort();
    setStreaming(false);
    setMessages(prev =>
      prev.map(m => m.streaming ? { ...m, streaming: false } : m),
    );
  }, []);

  const handleSend = useCallback(async (overrideInput?: string) => {
    const text = (overrideInput ?? input).trim();
    if (!text || streaming) return;

    const userMsg: Message = { id: genId(), role: 'user', content: text };
    const assistantId = genId();
    const assistantMsg: Message = {
      id: assistantId,
      role: 'assistant',
      content: '',
      streaming: true,
      sessionId: currentSessionId,
    };

    setMessages(prev => [...prev, userMsg, assistantMsg]);
    setInput('');
    setStreaming(true);

    const history = [
      ...messages.map(m => ({ role: m.role, content: m.content })),
      { role: 'user', content: text },
    ];

    const controller = new AbortController();
    abortRef.current = controller;

    let accumulatedContent = '';

    try {
      await streamChat(
        history,
        currentSessionId,
        division,
        {
          onDelta: delta => {
            accumulatedContent += delta;
            setMessages(prev =>
              prev.map(m =>
                m.id === assistantId ? { ...m, content: accumulatedContent } : m,
              ),
            );
          },
          onDone: ({ session_id, message_id, content, sources }) => {
            sessionPersistedRef.current = true;
            setMessages(prev =>
              prev.map(m =>
                m.id === assistantId
                  ? { ...m, streaming: false, dbId: message_id, sessionId: session_id, content: content ?? accumulatedContent, sources: sources ?? [] }
                  : m,
              ),
            );
            setStreaming(false);
            loadSessions();
          },
          onError: err => {
            setMessages(prev =>
              prev.map(m =>
                m.id === assistantId
                  ? { ...m, streaming: false, error: true, content: err || TIRED_MESSAGE }
                  : m,
              ),
            );
            setStreaming(false);
          },
        },
        controller.signal,
      );
    } catch (err: unknown) {
      if (err instanceof Error && err.name === 'AbortError') {
        return;
      }
      setMessages(prev =>
        prev.map(m =>
          m.id === assistantId
            ? { ...m, streaming: false, error: true, content: TIRED_MESSAGE }
            : m,
        ),
      );
      setStreaming(false);
    }
  }, [input, streaming, messages, currentSessionId, loadSessions, division]);

  const showSidebar = sidebarOpen && sessionsAvailable;

  return (
    <div className="flex h-full overflow-hidden">
      {showSidebar && (
        <ChatSidebar
          sessions={sessions}
          currentSessionId={currentSessionId}
          onSelectSession={handleSelectSession}
          onNewChat={handleNewChat}
          onDeleteSession={handleDeleteSession}
          loading={sessionsLoading}
          division={division}
          onDivisionChange={setDivision}
        />
      )}

      <div className="flex flex-col flex-1 min-w-0">
        <div
          className="flex items-center gap-2 px-4 py-2 border-b flex-shrink-0"
          style={{ borderColor: 'var(--color-border)', background: 'var(--color-bg-primary)' }}
        >
          {sessionsAvailable && (
            <button
              onClick={() => setSidebarOpen(o => !o)}
              className="w-7 h-7 rounded-lg flex items-center justify-center transition-all cursor-pointer"
              style={{ color: 'var(--color-text-muted)' }}
              title={sidebarOpen ? 'Hide sidebar' : 'Show sidebar'}
              onMouseEnter={e => (e.currentTarget.style.color = 'var(--color-text-primary)')}
              onMouseLeave={e => (e.currentTarget.style.color = 'var(--color-text-muted)')}
            >
              {sidebarOpen
                ? <PanelLeftClose className="h-4 w-4" />
                : <PanelLeftOpen className="h-4 w-4" />
              }
            </button>
          )}
          <span
            className="text-sm font-semibold"
            style={{ color: 'var(--color-text-heading)', fontFamily: 'var(--font-heading)' }}
          >
            {CHAT_HEADER}
          </span>
          {docsAsOf && (
            <>
              <span style={{ color: 'var(--color-border)' }}>·</span>
              <span
                className="text-xs"
                style={{ color: 'var(--color-text-muted)' }}
              >
                Documents as of {new Date(docsAsOf + 'T12:00:00').toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' })} (only documents after 2018 are taken into account)
              </span>
            </>
          )}
          <div className="flex-1" />
          {messages.length > 0 && (
            <button
              onClick={handleShare}
              disabled={streaming}
              className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs font-medium border transition-all cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
              title="Copy a read-only link to this conversation"
              style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-muted)' }}
              onMouseEnter={e => {
                (e.currentTarget as HTMLButtonElement).style.color = 'var(--color-text-primary)';
                (e.currentTarget as HTMLButtonElement).style.borderColor = 'var(--color-accent-primary)';
              }}
              onMouseLeave={e => {
                (e.currentTarget as HTMLButtonElement).style.color = 'var(--color-text-muted)';
                (e.currentTarget as HTMLButtonElement).style.borderColor = 'var(--color-border)';
              }}
            >
              <Share2 className="h-3.5 w-3.5" />
              Share
            </button>
          )}
          <WhatsNewButton tabId="chat" open={showWhatsNew} onClick={() => setShowWhatsNew(o => !o)} />
        </div>

        {showWhatsNew && <WhatsNewModal tabId="chat" onClose={() => setShowWhatsNew(false)} />}

        <div className="flex-1 overflow-y-auto py-4">
          {messages.length === 0 ? (
            <WelcomeState title={CHAT_TITLE} onPrompt={p => handleSend(p)} />
          ) : (
            <>
              {messages.map(msg => (
                <ChatMessage
                  key={msg.id}
                  message={msg}
                  showFeedback={msg.role === 'assistant' && !msg.streaming && !msg.error}
                  allMessages={messages}
                />
              ))}
              <div ref={messagesEndRef} />
            </>
          )}
        </div>

        <ChatInput
          value={input}
          onChange={setInput}
          onSend={() => handleSend()}
          onStop={handleStop}
          disabled={streaming}
          streaming={streaming}
          placeholder={
            division === 'ALL'
              ? 'Ask a question about your documents…'
              : `Ask a question (scope: ${division} division)…`
          }
        />
      </div>

      <style>{`
        @keyframes blink {
          0%, 100% { opacity: 1; }
          50% { opacity: 0; }
        }
        @keyframes bounce {
          0%, 100% { transform: translateY(0); }
          50% { transform: translateY(-4px); }
        }
      `}</style>
    </div>
  );
}