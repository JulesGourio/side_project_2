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

/** Time of the last write of any comparison key (epoch ms, as a string). */
const LS_SAVED_AT = 'compare_saved_at';

/** What the browser keeps of a comparison is dropped this long after its last write; the history in the database keeps the rest. */
export const LS_MAX_AGE_MS = 7 * 24 * 3600 * 1000;

/** While a report streams in, persist it at most this often. */
export const LS_STREAM_PERSIST_MS = 1000;

export function lsGet(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

// localStorage quota is small (two documents can fill it): a failed write must never fail an analysis.
export function lsSet(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
    localStorage.setItem(LS_SAVED_AT, String(Date.now()));
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

/** Remove every comparison key (documents, reports, hashes, votes) when the last write is older than `LS_MAX_AGE_MS`. */
export function clearExpiredStorage(now: number = Date.now()): void {
  try {
    const savedAt = Number(localStorage.getItem(LS_SAVED_AT));
    if (!savedAt) {
      localStorage.setItem(LS_SAVED_AT, String(now)); // data stored before the timestamp existed starts its 7 days now
      return;
    }
    if (now - savedAt <= LS_MAX_AGE_MS) return;
    localStorage.removeItem(LS_SAVED_AT);
    for (const key of Object.values(LS)) localStorage.removeItem(key);
  } catch { /* storage unavailable */ }
}

// Runs when the module loads, before any hook reads the stored documents.
clearExpiredStorage();

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
