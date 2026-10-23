import { beforeEach, describe, expect, test } from 'bun:test';

const store = new Map<string, string>();
(globalThis as { localStorage?: unknown }).localStorage = {
  getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
  setItem: (k: string, v: string) => void store.set(k, v),
  removeItem: (k: string) => void store.delete(k),
};

const { LS, LS_MAX_AGE_MS, clearExpiredStorage, lsSet } = await import('../src/components/compare/storage');

const DAY = 24 * 3600 * 1000;

beforeEach(() => store.clear());

describe('clearExpiredStorage', () => {
  test('keeps the comparison for 7 days after its last write', () => {
    const now = Date.now();
    lsSet(LS.analysis, 'report');
    clearExpiredStorage(now + LS_MAX_AGE_MS - DAY);
    expect(store.get(LS.analysis)).toBe('report');
  });

  test('removes every comparison key after 7 days', () => {
    const now = Date.now();
    lsSet(LS.analysis, 'report');
    lsSet(LS.oldHash, 'abc');
    clearExpiredStorage(now + LS_MAX_AGE_MS + DAY);
    expect(store.has(LS.analysis)).toBe(false);
    expect(store.has(LS.oldHash)).toBe(false);
  });

  test('data stored without a timestamp starts its 7 days now', () => {
    store.set(LS.analysis, 'report');
    clearExpiredStorage(Date.now());
    expect(store.get(LS.analysis)).toBe('report');
    expect(store.has('compare_saved_at')).toBe(true);
  });
});
