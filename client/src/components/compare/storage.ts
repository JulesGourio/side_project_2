import type { DocFile } from './types';
import { b64ToBytes } from './files';

export const LS = {
  oldPdf: 'compare_old_pdf',
  newPdf: 'compare_new_pdf',
  analysis: 'compare_analysis',
  analysisStd: 'compare_analysis_std',
  messageId: 'compare_message_id',
  messageIdStd: 'compare_message_id_std',
  impact: 'compare_impact_v5',
  oldHash: 'compare_old_hash',
  newHash: 'compare_new_hash',
  feedbackSubmitted: 'compare_feedback_submitted_v2', // JSON string[]
  impactVotes: 'compare_impact_votes', // { "<request id>:<ref>": "up" | "down" }
} as const;

/** While a report streams in, persist it at most this often. */
export const LS_STREAM_PERSIST_MS = 1000;

export function lsGet(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

/**
 * localStorage is close to full as soon as two documents are persisted in it;
 * a failed write must never turn a finished analysis into an error.
 */
export function lsSet(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // quota exceeded — the in-memory state is still correct
  }
}

export function lsRemove(...keys: string[]): void {
  try {
    for (const key of keys) localStorage.removeItem(key);
  } catch { /* storage unavailable */ }
}

export function lsGetJson<T>(key: string): T | null {
  try {
    const raw = lsGet(key);
    return raw ? (JSON.parse(raw) as T) : null;
  } catch {
    return null;
  }
}

export function isFeedbackSubmitted(key: string): boolean {
  return (lsGetJson<string[]>(LS.feedbackSubmitted) ?? []).includes(key);
}

export function markFeedbackSubmitted(key: string): void {
  const set = lsGetJson<string[]>(LS.feedbackSubmitted) ?? [];
  if (!set.includes(key)) set.push(key);
  lsSet(LS.feedbackSubmitted, JSON.stringify(set.slice(-200)));
}

export function savePdfToStorage(key: string, pdf: DocFile | null): void {
  if (pdf) lsSet(key, JSON.stringify({ name: pdf.name, size: pdf.size, data: pdf.data, mimeType: pdf.mimeType }));
  else lsRemove(key);
}

export function loadPdfFromStorage(key: string): DocFile | null {
  const parsed = lsGetJson<{ name: string; size: number; data: string; mimeType?: string }>(key);
  if (!parsed) return null;
  try {
    return { name: parsed.name, size: parsed.size, data: parsed.data, mimeType: parsed.mimeType || 'application/octet-stream', bytes: b64ToBytes(parsed.data) };
  } catch {
    return null;
  }
}
