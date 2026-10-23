import { describe, expect, test } from 'bun:test';

import { buildHistory } from '../src/components/chat/history';
import type { Message } from '../src/components/chat/message';

const user = (content: string): Message => ({ id: content, role: 'user', content });
const assistant = (content: string, extra: Partial<Message> = {}): Message => ({
  id: `a-${content}`, role: 'assistant', content, ...extra,
});

describe('buildHistory', () => {
  test('keeps complete turns and ends with the new question', () => {
    const turns = buildHistory([user('q1'), assistant('a1')], 'q2');
    expect(turns.map((t) => t.content)).toEqual(['q1', 'a1', 'q2']);
  });

  test('drops a question together with its failed answer', () => {
    const turns = buildHistory([user('q1'), assistant('oops', { error: true }), user('q2'), assistant('a2')], 'q3');
    expect(turns.map((t) => t.content)).toEqual(['q2', 'a2', 'q3']);
  });

  test('drops a question together with its stopped or empty answer', () => {
    expect(buildHistory([user('q1'), assistant('partial', { stopped: true })], 'q2').map((t) => t.content)).toEqual(['q2']);
    expect(buildHistory([user('q1'), assistant('  ')], 'q2').map((t) => t.content)).toEqual(['q2']);
  });

  test('never sends two questions in a row', () => {
    const roles = buildHistory([user('q1'), assistant('x', { error: true }), user('q2'), assistant('y', { stopped: true })], 'q3')
      .map((t) => t.role);
    expect(roles).toEqual(['user']);
  });
});
