

export interface DocFile {
  name: string;
  size: number;
  data: string;
  bytes: Uint8Array<ArrayBuffer>;
  mimeType: string;
}

export const MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024;

export const DOCX_MIME_TYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';

export const SUPPORTED_EXTENSIONS = new Set([
  'pdf',
  'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'tiff', 'tif',
  'docx', 'doc',
  'pptx', 'ppt',
  'xlsx', 'xls',
  'xml',
]);


export const FILE_ACCEPT = [
  'application/pdf',
  'image/jpeg', 'image/png', 'image/gif', 'image/webp', 'image/bmp', 'image/tiff',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'application/msword',
  'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  'application/vnd.ms-powerpoint',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  'application/vnd.ms-excel',
  '.pdf', '.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tiff', '.tif',
  '.docx', '.doc', '.pptx', '.ppt', '.xlsx', '.xls', '.xml',
].join(',');

export const LS_OLD_PDF = 'compare_old_pdf';
export const LS_NEW_PDF = 'compare_new_pdf';
export const LS_ANALYSIS = 'compare_analysis';
export const LS_ANALYSIS_STD = 'compare_analysis_std';
export const LS_MESSAGE_ID_STD = 'compare_message_id_std';
export const LS_IMPACT = 'compare_impact_v5';
export const LS_SESSION_PATH = 'compare_session_path';
export const LS_MESSAGE_ID = 'compare_message_id';
export const LS_OLD_HASH = 'compare_old_hash';
export const LS_NEW_HASH = 'compare_new_hash';
export const LS_FEEDBACK_SUBMITTED = 'compare_feedback_submitted_v2'; // JSON string[]
export const LS_PROCESSING_METHOD = 'compare_processing_method';
export const LS_IMPACT_MODE = 'compare_impact_mode';
export const LS_IMPACT_MANUAL_TEXT = 'compare_impact_manual_text';
// The "describe a change by hand, no files" mode is hidden (too niche); it still works end to end behind this flag.
export const IMPACT_MANUAL_MODE_ENABLED = false;

export function isFeedbackSubmitted(key: string): boolean {
  try {
    const raw = localStorage.getItem(LS_FEEDBACK_SUBMITTED);
    const set: string[] = raw ? JSON.parse(raw) : [];
    return set.includes(key);
  } catch { return false; }
}

export function markFeedbackSubmitted(key: string): void {
  try {
    const raw = localStorage.getItem(LS_FEEDBACK_SUBMITTED);
    const set: string[] = raw ? JSON.parse(raw) : [];
    if (!set.includes(key)) {
      set.push(key);
      // Cap to avoid localStorage bloat
      if (set.length > 200) set.splice(0, set.length - 200);
    }
    lsSet(LS_FEEDBACK_SUBMITTED, JSON.stringify(set));
  } catch {}
}



export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// localStorage is close to full as soon as two documents are persisted in it;
// a failed write must never turn a finished analysis into an error.
export function lsSet(key: string, value: string) {
  try {
    localStorage.setItem(key, value);
  } catch {
    // quota exceeded — the in-memory state is still correct
  }
}

// While a report streams in, persist it at most this often.
export const LS_STREAM_PERSIST_MS = 1000;

export const STREAM_INTERRUPTED = 'Connection lost before the report was complete. Please retry.';

export async function computeFileHash(bytes: Uint8Array<ArrayBuffer>): Promise<string> {
  const buffer = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(buffer))
    .map(b => b.toString(16).padStart(2, '0'))
    .join('');
}

export function readFile(file: File): Promise<{ base64: string; bytes: Uint8Array<ArrayBuffer>; mimeType: string }> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result as string;
      const base64 = result.split(',')[1];
      const binary = atob(base64);
      const buffer = new ArrayBuffer(binary.length);
      const bytes = new Uint8Array(buffer);
      for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
      resolve({ base64, bytes, mimeType: file.type || 'application/octet-stream' });
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

export function savePdfToStorage(key: string, pdf: Omit<DocFile, 'bytes'> | null) {
  if (pdf) {
    try {
      lsSet(key, JSON.stringify({ name: pdf.name, size: pdf.size, data: pdf.data, mimeType: pdf.mimeType }));
    } catch {
      // localStorage full — silently ignore
    }
  } else {
    localStorage.removeItem(key);
  }
}

export function loadPdfFromStorage(key: string): DocFile | null {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    const binary = atob(parsed.data);
    const buffer = new ArrayBuffer(binary.length);
    const bytes = new Uint8Array(buffer);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return { name: parsed.name, size: parsed.size, data: parsed.data, mimeType: parsed.mimeType || 'application/octet-stream', bytes };
  } catch {
    return null;
  }
}




export interface FeedbackProps {
  submissionKey: string;
  messageId: number | null;
}




export interface DiffItem {
  section?: string;
  page?: string;
  type?: string;
  criticality?: string;
  before?: string;
  after?: string;
  rationale?: string;
}

export interface ImagePair {
  status: 'modified' | 'added' | 'removed' | 'orientation_changed';
  old_page: number | null;
  new_page: number | null;
  old_b64: string | null;
  new_b64: string | null;
  index: number;
}



export interface DocSummaryMeta {
  truncated: boolean;
  durationS: number;
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  costEur: number;
}

export interface DocSummaryState {
  text: string;
  error: string;
  loading: boolean;
  noContent: boolean;
  meta: DocSummaryMeta | null;
}

export const EMPTY_SUMMARY: DocSummaryState = { text: '', error: '', loading: false, noContent: false, meta: null };
