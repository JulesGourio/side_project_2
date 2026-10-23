import type { Message } from './message';

export interface HistoryTurn {
  role: 'user' | 'assistant';
  content: string;
}

/** An answer the model must not see again: failed, stopped before its end, or empty. */
function isUnusable(m: Message): boolean {
  return m.role === 'assistant' && (!!m.error || !!m.stopped || !m.content.trim());
}

/**
 * The conversation sent to the server: a question is dropped together with the answer that failed or was stopped,
 * so the server never sees two questions in a row. The new question is always the last turn.
 *
 * @param messages the conversation on screen, oldest first, without the new question
 * @param question the new question
 * @returns the turns to send, oldest first
 */
export function buildHistory(messages: Message[], question: string): HistoryTurn[] {
  const turns: HistoryTurn[] = [];
  for (let i = 0; i < messages.length; i++) {
    const m = messages[i];
    if (m.role === 'user' && messages[i + 1] && isUnusable(messages[i + 1])) {
      i += 1;
      continue;
    }
    if (isUnusable(m) || !m.content.trim()) continue;
    turns.push({ role: m.role, content: m.content });
  }
  turns.push({ role: 'user', content: question });
  return turns;
}
