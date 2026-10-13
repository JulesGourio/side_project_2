import type { Division } from './division';
import type { Source } from './message';

const WS_PATH = '/api/chat/ws';

/** The only failure text a user sees (same as the server's TIRED_MESSAGE): the cause is logged server-side. */
export const TIRED_MESSAGE = 'Qualibot is a bit tired right now. Please wait a moment and try again.';

/** A socket that fails before the question was sent is opened again this many times. */
const CONNECT_RETRIES = 2;

export interface StreamCallbacks {
  onDelta: (text: string) => void;
  onDone: (data: { session_id: string; message_id?: number; content?: string; sources?: Source[] }) => void;
  onError: (err: string) => void;
}

/**
 * Stream one chat turn over the WebSocket. A turn always ends with exactly one outcome — done,
 * error or user abort — so a socket that closes without one cannot leave the message on "Thinking".
 */
export function streamChat(
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
    let settled = false;

    const finish = () => { settled = true; resolve(); };
    // Nothing reached the server yet: open a new socket instead of failing the turn.
    const retryConnect = () => {
      settled = true;
      setTimeout(() => {
        if (signal.aborted) { resolve(); return; }
        streamChat(messages, sessionId, division, callbacks, signal, attempt + 1).then(resolve);
      }, 1000 * (attempt + 1));
    };
    const fail = () => {
      if (settled) return;
      if (!sent && attempt < CONNECT_RETRIES) { retryConnect(); return; }
      callbacks.onError(TIRED_MESSAGE);
      finish();
    };

    signal.addEventListener('abort', () => {
      settled = true;
      try { ws.close(); } catch { /* ignore */ }
      resolve();
    });

    ws.onopen = () => {
      ws.send(JSON.stringify({ messages, session_id: sessionId, division }));
      sent = true;
    };

    ws.onmessage = (event) => {
      try {
        const parsed = JSON.parse(event.data as string);
        if (parsed.type === 'delta') {
          callbacks.onDelta(parsed.delta ?? '');
        } else if (parsed.type === 'done') {
          callbacks.onDone({ session_id: parsed.session_id, message_id: parsed.message_id, content: parsed.content, sources: parsed.sources ?? [] });
          finish();
        } else if (parsed.type === 'error') {
          callbacks.onError(parsed.error || TIRED_MESSAGE);
          finish();
        }
      } catch {
        // ignore malformed frames
      }
    };

    ws.onerror = fail;
    ws.onclose = fail;
  });
}
