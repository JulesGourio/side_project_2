import { useCallback, useRef, useState } from 'react';
import { toast } from 'sonner';
import { TIRED_MESSAGE, streamChat } from './chatStream';
import { stripDivision, type Division } from './division';
import type { Message, Source } from './message';
import { buildHistory } from './history';

const genId = () => `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
const genSessionId = () => (typeof crypto !== 'undefined' && crypto.randomUUID
  ? crypto.randomUUID()
  : `${Date.now()}-${Math.random().toString(36).slice(2)}`);

export function useChatConversation(division: Division, onTurnSaved: () => void) {
  const [sessionId, setSessionId] = useState(genSessionId);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [streaming, setStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const persistedRef = useRef(false);
  const selectSeqRef = useRef(0);

  const patchMessage = (id: string, patch: Partial<Message>) =>
    setMessages(prev => prev.map(m => (m.id === id ? { ...m, ...patch } : m)));

  const newChat = useCallback(() => {
    selectSeqRef.current++;
    if (streaming) abortRef.current?.abort();
    setSessionId(genSessionId());
    persistedRef.current = false;
    setMessages([]);
    setInput('');
    setStreaming(false);
  }, [streaming]);

  const select = useCallback(async (id: string) => {
    if (streaming) return;
    const seq = ++selectSeqRef.current;
    try {
      const res = await fetch(`/api/chat/sessions/${id}`);
      if (!res.ok) { toast.error('Failed to load conversation'); return; }
      const data = await res.json();
      if (seq !== selectSeqRef.current) return;
      setSessionId(id);
      persistedRef.current = true;
      setMessages(
        (data.messages ?? []).map((m: { id: number; role: 'user' | 'assistant'; content: string; sources?: Source[] }) => ({
          id: String(m.id),
          role: m.role,
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

  const stop = useCallback(() => {
    abortRef.current?.abort();
    setStreaming(false);
    setMessages(prev => prev.map(m => (m.streaming ? { ...m, streaming: false, stopped: true } : m)));
  }, []);

  const share = useCallback(async () => {
    if (!persistedRef.current) return;
    try {
      const res = await fetch(`/api/chat/sessions/${sessionId}/share`, { method: 'POST' });
      if (!res.ok) { toast.error('Failed to create the share link'); return; }
      const data = await res.json();
      await navigator.clipboard.writeText(`${location.origin}/chat/shared/${data.share_token}`);
      toast.success('Share link copied to clipboard');
    } catch {
      toast.error('Failed to create the share link');
    }
  }, [sessionId]);

  const send = async (override?: string) => {
    const text = (override ?? input).trim();
    if (!text || streaming) return;

    const assistantId = genId();
    setMessages(prev => [
      ...prev,
      { id: genId(), role: 'user', content: text },
      { id: assistantId, role: 'assistant', content: '', streaming: true, sessionId },
    ]);
    setInput('');
    setStreaming(true);

    const history = buildHistory(messages, text);
    const controller = new AbortController();
    abortRef.current = controller;
    let accumulated = '';

    try {
      await streamChat(history, sessionId, division, {
        onDelta: delta => {
          accumulated += delta;
          patchMessage(assistantId, { content: accumulated });
        },
        onDone: ({ session_id, message_id, content, sources }) => {
          persistedRef.current = true;
          patchMessage(assistantId, { streaming: false, dbId: message_id, sessionId: session_id, content: content ?? accumulated, sources: sources ?? [] });
          setStreaming(false);
          onTurnSaved();
        },
        onStatus: status => patchMessage(assistantId, { status }),
        onError: err => {
          patchMessage(assistantId, { streaming: false, error: true, content: err || TIRED_MESSAGE });
          setStreaming(false);
        },
      }, controller.signal);
    } catch (err) {
      if (err instanceof Error && err.name === 'AbortError') return;
      patchMessage(assistantId, { streaming: false, error: true, content: TIRED_MESSAGE });
      setStreaming(false);
    }
  };

  return { sessionId, messages, input, setInput, streaming, send, stop, newChat, select, share };
}
