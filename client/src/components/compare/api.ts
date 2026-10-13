import { toast } from 'sonner';
import type { DiffItem, DocFile, ImagePair, TokenUsage } from './types';
import { b64ToBytes, docBlob } from './files';

export const errorMessage = (e: unknown): string => (e instanceof Error ? e.message : String(e));

export const STREAM_INTERRUPTED = 'Connection lost before the report was complete. Please retry.';

/** Extract complete JSON objects from a partial/streaming JSON array string. */
export function parsePartialJsonItems(text: string): DiffItem[] {
  const items: DiffItem[] = [];
  let depth = 0;
  let start = -1;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (ch === '{') {
      if (depth === 0) start = i;
      depth++;
    } else if (ch === '}') {
      depth--;
      if (depth === 0 && start !== -1) {
        try {
          const obj = JSON.parse(text.slice(start, i + 1));
          if (typeof obj === 'object' && obj !== null) items.push(obj as DiffItem);
        } catch { /* skip malformed */ }
        start = -1;
      }
    }
  }
  return items;
}

export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

/** Throws with the server's `{error}` message when the response is not OK. */
export async function ensureOk(res: Response): Promise<Response> {
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
    throw new Error(err.error ?? res.statusText);
  }
  return res;
}

/** POST a form to an export endpoint and save the response as a file; failures surface as a toast. */
export async function downloadExport(endpoint: string, form: FormData, filename: string, label: string): Promise<void> {
  try {
    const res = await ensureOk(await fetch(endpoint, { method: 'POST', body: form }));
    saveBlob(await res.blob(), filename);
  } catch (e) {
    toast.error(`${label} export failed: ${errorMessage(e)}`);
  }
}

export function exportChangeTableExcel(jsonText: string, base: string, fileType: string, images: ImagePair[]): Promise<void> {
  const form = new FormData();
  form.append('json_text', jsonText);
  form.append('filename', `analysis_${base}`);
  form.append('file_type', fileType);
  if (images.length > 0) form.append('image_pairs_json', JSON.stringify(images));
  return downloadExport('/api/compare/export-excel', form, `analysis_${base}.xlsx`, 'Excel');
}

// ---------------------------------------------------------------------------
// Session auto-save (Unity Catalog volume) — all best-effort
// ---------------------------------------------------------------------------

export async function autoSaveDocs(old: DocFile, nw: DocFile): Promise<string> {
  try {
    const form = new FormData();
    form.append('old_file', docBlob(old), old.name);
    form.append('new_file', docBlob(nw), nw.name);
    const res = await fetch('/api/compare/save', { method: 'POST', body: form });
    const data = await res.json();
    if (data.session_path) return data.session_path;
    if (data.error) toast.warning(`Auto-save skipped: ${data.error}`);
  } catch (e) {
    toast.warning(`Auto-save unavailable: ${errorMessage(e)}`);
  }
  return '';
}

export async function autoSaveResult(path: string, filename: string, content: string): Promise<void> {
  if (!path || !content.trim()) return;
  try {
    const form = new FormData();
    form.append('session_path', path);
    form.append('filename', filename);
    form.append('content', content);
    const res = await fetch('/api/compare/save-result', { method: 'POST', body: form });
    const data = await res.json();
    if (data.error) toast.warning(`Could not save ${filename}: ${data.error}`);
  } catch (e) {
    toast.warning(`Could not save ${filename}: ${errorMessage(e)}`);
  }
}

/** Fire-and-forget save of a derived artifact (Excel/PDF) next to the session files. */
function autoSaveDerived(endpoint: string, label: string, fields: Record<string, string>): void {
  const form = new FormData();
  for (const [k, v] of Object.entries(fields)) form.append(k, v);
  form.append('filename', 'analysis');
  fetch(endpoint, { method: 'POST', body: form })
    .then(r => r.json())
    .then(d => { if (d.error) toast.warning(`Could not save ${label}: ${d.error}`); })
    .catch(() => { /* best-effort */ });
}

export function autoSaveExcel(path: string, jsonText: string, fileType: string, imagePairs: ImagePair[]): void {
  autoSaveDerived('/api/compare/save-excel', 'Excel', {
    session_path: path,
    json_text: jsonText,
    file_type: fileType,
    ...(imagePairs.length > 0 && { image_pairs_json: JSON.stringify(imagePairs) }),
  });
}

export function autoSavePdf(path: string, markdown: string, oldName: string, newName: string): void {
  autoSaveDerived('/api/compare/save-pdf', 'PDF', {
    session_path: path,
    markdown_text: markdown,
    title: newName.replace(/\.[^.]+$/, '') || 'Change summary',
    old_name: oldName,
    new_name: newName,
  });
}

// ---------------------------------------------------------------------------
// History
// ---------------------------------------------------------------------------

export interface HistorySaveInput {
  oldName: string;
  newName: string;
  analysisText: string;
  volumePath: string;
  oldHash: string;
  newHash: string;
  fileType: string;
  processingMethod: string;
  processorVersion: string;
  ttftS: number;
  generationS: number;
  usage: TokenUsage;
  llmRequestId: number | null;
}

/** Returns the new `messages` row id, or null when history is unavailable. */
export async function saveToHistory(h: HistorySaveInput): Promise<number | null> {
  try {
    const res = await fetch('/api/history', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        old_filename: h.oldName,
        new_filename: h.newName,
        old_file_hash: h.oldHash || null,
        new_file_hash: h.newHash || null,
        analysis_text: h.analysisText || null,
        impact_text: null,
        volume_session_path: h.volumePath || null,
        file_type: h.fileType || null,
        processing_method: h.processingMethod || null,
        processor_version: h.processorVersion || null,
        ttft_s: h.ttftS || null,
        generation_s: h.generationS || null,
        input_tokens: h.usage.input_tokens ?? null,
        output_tokens: h.usage.output_tokens ?? null,
        thinking_tokens: h.usage.thinking_tokens ?? null,
        total_tokens: h.usage.total_tokens ?? null,
        cost_eur: h.usage.cost_eur ?? null,
        llm_request_id: h.llmRequestId,
      }),
    });
    const data = await res.json();
    return typeof data.messages_id === 'number' ? data.messages_id : null;
  } catch {
    return null;
  }
}

/** Kept with the comparison it was run on, so reopening the history entry shows it again. */
export function saveImpactToHistory(historyId: number, impactJson: string): void {
  fetch(`/api/history/${historyId}/impact`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ impact_json: impactJson }),
  }).catch(() => { /* best-effort — the result stays on screen and in the server cache */ });
}

/** Restore the two original files of a past comparison from its volume session folder. */
export async function loadSessionFiles(
  sessionPath: string, oldName: string, newName: string,
): Promise<{ old?: DocFile; new?: DocFile }> {
  try {
    const params = new URLSearchParams({ session_path: sessionPath, old_filename: oldName, new_filename: newName });
    const res = await fetch(`/api/compare/load?${params}`);
    if (!res.ok) return {};
    const data = await res.json();
    const toDoc = (f?: { name: string; size: number; data: string; mimeType: string }): DocFile | undefined =>
      f && { name: f.name, size: f.size, data: f.data, bytes: b64ToBytes(f.data), mimeType: f.mimeType };
    return { old: toDoc(data.old), new: toDoc(data.new) };
  } catch {
    return {}; // best-effort — the caller keeps the name-only placeholders
  }
}

// ---------------------------------------------------------------------------
// Analysis stream (SSE from POST /api/compare/analyze)
// ---------------------------------------------------------------------------

export interface AnalysisStreamResult {
  text: string;
  fileType: string;
  method: string;
  usage: TokenUsage;
  llmRequestId: number | null;
  firstTokenAt: number | null;
  /** The server flagged the report as possibly incomplete. */
  warned: boolean;
  /** Set when the server answered from its DB cache. */
  cached: { messageId: number; sessionPath: string } | null;
  /** The `[DONE]` marker arrived; false means the gateway cut the stream. */
  complete: boolean;
}

interface AnalysisStreamHandlers {
  onText: (accumulated: string) => void;
  onFileType: (fileType: string) => void;
  onImages?: (images: ImagePair[]) => void;
}

export async function readAnalysisStream(res: Response, on: AnalysisStreamHandlers): Promise<AnalysisStreamResult> {
  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  const out: AnalysisStreamResult = {
    text: '', fileType: '', method: 'structured', usage: {}, llmRequestId: null,
    firstTokenAt: null, warned: false, cached: null, complete: false,
  };
  let buffer = '';
  while (!out.complete) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() ?? '';
    for (const line of lines) {
      if (!line.startsWith('data: ')) continue;
      const data = line.slice(6).trim();
      if (data === '[DONE]') { out.complete = true; break; }
      let event;
      try { event = JSON.parse(data); } catch { continue; /* skip malformed chunks */ }
      if (event.type === 'response.output_text.delta') {
        out.firstTokenAt ??= Date.now();
        out.text += event.delta;
        on.onText(out.text);
      } else if (event.type === 'metadata') {
        out.fileType = event.file_type || '';
        on.onFileType(out.fileType);
        out.method = event.method || 'structured';
        out.llmRequestId = event.llm_request_id ?? null;
        if (event.cached && event.message_id != null) out.cached = { messageId: event.message_id, sessionPath: event.session_path ?? '' };
      } else if (event.type === 'image_context') {
        on.onImages?.(event.images || []);
      } else if (event.type === 'warning') {
        out.warned = true;
        toast.warning(event.detail || 'The report may be incomplete.');
      } else if (event.type === 'usage') {
        out.usage = event;
      } else if (event.type === 'error') {
        throw new Error(event.error ?? 'Unknown server error');
      }
    }
  }
  return out;
}
