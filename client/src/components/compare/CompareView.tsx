import { useState, useRef, useCallback, useEffect } from 'react';
import {
  Upload,
  FileText,
  X,
  Loader2,
  Trash2,
  ArrowRight,
  RotateCcw,
  AlertCircle,
  Download,
  CheckCircle2,
  History,
  ThumbsUp,
  ThumbsDown,
  Eye,
  Send,
  Square,
  Database,
  Info,
} from 'lucide-react';
import { toast } from 'sonner';
import { MarkdownRenderer } from '@/components/shared/MarkdownRenderer';
import { ExactDocxPreview } from '@/components/shared/ExactDocxPreview';
import { getAppConfig, getProcessorsConfig, type ProcessorVersion } from '@/lib/config';
import { ComparisonHistory, type FullComparison } from './ComparisonHistory';
import { WhatsNewButton, WhatsNewModal } from '@/components/layout/WhatsNewBanner';
import { ImpactResultsCard, type ImpactResult } from './ImpactResults';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface DocFile {
  name: string;
  size: number;
  data: string;
  bytes: Uint8Array<ArrayBuffer>;
  mimeType: string;
}

const MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024;

const DOCX_MIME_TYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';

const SUPPORTED_EXTENSIONS = new Set([
  'pdf',
  'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'tiff', 'tif',
  'docx', 'doc',
  'pptx', 'ppt',
  'xlsx', 'xls',
  'xml',
]);


const FILE_ACCEPT = [
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

const LS_OLD_PDF = 'compare_old_pdf';
const LS_NEW_PDF = 'compare_new_pdf';
const LS_ANALYSIS = 'compare_analysis';
const LS_ANALYSIS_STD = 'compare_analysis_std';
const LS_MESSAGE_ID_STD = 'compare_message_id_std';
const LS_IMPACT = 'compare_impact_v5';
const LS_SESSION_PATH = 'compare_session_path';
const LS_MESSAGE_ID = 'compare_message_id';
const LS_OLD_HASH = 'compare_old_hash';
const LS_NEW_HASH = 'compare_new_hash';
const LS_FEEDBACK_SUBMITTED = 'compare_feedback_submitted_v2'; // JSON string[]
const LS_PROCESSING_METHOD = 'compare_processing_method';
const LS_IMPACT_MODE = 'compare_impact_mode';
const LS_IMPACT_MANUAL_TEXT = 'compare_impact_manual_text';
// Manual mode UI hidden for now (business feedback: too niche/confusing) — logic kept intact behind this flag.
const IMPACT_MANUAL_MODE_ENABLED = false;

function isFeedbackSubmitted(key: string): boolean {
  try {
    const raw = localStorage.getItem(LS_FEEDBACK_SUBMITTED);
    const set: string[] = raw ? JSON.parse(raw) : [];
    return set.includes(key);
  } catch { return false; }
}

function markFeedbackSubmitted(key: string): void {
  try {
    const raw = localStorage.getItem(LS_FEEDBACK_SUBMITTED);
    const set: string[] = raw ? JSON.parse(raw) : [];
    if (!set.includes(key)) {
      set.push(key);
      // Cap to avoid localStorage bloat
      if (set.length > 200) set.splice(0, set.length - 200);
    }
    localStorage.setItem(LS_FEEDBACK_SUBMITTED, JSON.stringify(set));
  } catch {}
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

async function computeFileHash(bytes: Uint8Array<ArrayBuffer>): Promise<string> {
  const buffer = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(buffer))
    .map(b => b.toString(16).padStart(2, '0'))
    .join('');
}

function readFile(file: File): Promise<{ base64: string; bytes: Uint8Array<ArrayBuffer>; mimeType: string }> {
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

function savePdfToStorage(key: string, pdf: Omit<DocFile, 'bytes'> | null) {
  if (pdf) {
    try {
      localStorage.setItem(key, JSON.stringify({ name: pdf.name, size: pdf.size, data: pdf.data, mimeType: pdf.mimeType }));
    } catch {
      // localStorage full — silently ignore
    }
  } else {
    localStorage.removeItem(key);
  }
}

function loadPdfFromStorage(key: string): DocFile | null {
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


// ---------------------------------------------------------------------------
// PdfDropZone
// ---------------------------------------------------------------------------

function PdfDropZone({
  label,
  step,
  file,
  onFile,
  onRemove,
  onInvalidFile,
  disabled,
}: {
  label: string;
  step: number;
  file: DocFile | null;
  onFile: (f: DocFile) => void;
  onRemove: () => void;
  onInvalidFile: (msg: string) => void;
  disabled: boolean;
}) {
  const [isDragOver, setIsDragOver] = useState(false);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewHtml, setPreviewHtml] = useState<string | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  // Reset cached preview whenever a different file is loaded
  const prevFileKeyRef = useRef<string | null>(null);
  const fileKey = file ? `${file.name}:${file.size}` : null;
  if (fileKey !== prevFileKeyRef.current) {
    prevFileKeyRef.current = fileKey;
    if (previewHtml !== null) setPreviewHtml(null);
    if (previewLoading) setPreviewLoading(false);
  }

  const isDocxType = file ? file.mimeType === DOCX_MIME_TYPE : false;
  const isDocType = file
    ? file.mimeType !== 'application/pdf' && !file.mimeType.startsWith('image/') && !isDocxType
    : false;

  const openPreview = useCallback(async () => {
    setPreviewOpen(true);
    // .docx renders client-side (DocxPreview) — no server round-trip needed.
    if (!file || !isDocType || previewHtml) return;
    setPreviewLoading(true);
    try {
      const fd = new FormData();
      fd.append('file', new Blob([file.bytes], { type: file.mimeType }), file.name);
      const res = await fetch('/api/preview', { method: 'POST', body: fd });
      if (res.ok) setPreviewHtml(await res.text());
    } catch { /* best-effort */ }
    finally { setPreviewLoading(false); }
  }, [file, isDocType, previewHtml]);

  const handleFile = useCallback(
    async (f: File) => {
      const ext = f.name.includes('.') ? f.name.split('.').pop()?.toLowerCase() : '';
      if (!ext || !SUPPORTED_EXTENSIONS.has(ext)) {
        onInvalidFile(`Unsupported file type ".${ext || '?'}". Supported: PDF, images, DOCX, PPTX, XML.`);
        return;
      }
      if (f.size > MAX_FILE_SIZE_BYTES) {
        onInvalidFile('Each file must be 20 MB or smaller.');
        return;
      }
      const { base64, bytes, mimeType } = await readFile(f);
      onFile({ name: f.name, size: f.size, data: base64, bytes, mimeType });
    },
    [onFile, onInvalidFile],
  );

  const onDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setIsDragOver(false);
      const f = e.dataTransfer.files?.[0];
      if (f) handleFile(f);
    },
    [handleFile],
  );

  // Uploaded state
  if (file) {
    const isPlaceholder = !file.data;
    const borderColor = isPlaceholder
      ? (isDragOver ? '#d97706' : 'rgba(217,119,6,0.4)')
      : (isDragOver ? 'var(--color-accent-primary)' : 'rgba(var(--color-accent-primary-rgb,0,85,164),0.3)');
    const bgColor = isPlaceholder
      ? (isDragOver ? 'rgba(217,119,6,0.12)' : 'rgba(217,119,6,0.05)')
      : (isDragOver ? 'rgba(var(--color-accent-primary-rgb,0,85,164),0.1)' : 'rgba(var(--color-accent-primary-rgb,0,85,164),0.05)');
    return (
      <>
        <div
          className="flex-1 min-w-0 rounded-2xl p-4 transition-all"
          style={{ border: `1px solid ${borderColor}`, background: bgColor, cursor: disabled ? 'default' : 'copy' }}
          onDrop={disabled ? undefined : onDrop}
          onDragOver={disabled ? undefined : (e) => { e.preventDefault(); setIsDragOver(true); }}
          onDragLeave={disabled ? undefined : () => setIsDragOver(false)}
        >
          {isPlaceholder && (
            <p className="text-xs font-semibold mb-2" style={{ color: '#d97706' }}>
              Drop file to re-upload
            </p>
          )}
          <div className="flex items-center gap-3">
            {/* Step badge */}
            <div
              className="flex-shrink-0 w-8 h-8 rounded-full flex items-center justify-center"
              style={{ background: isPlaceholder ? '#d97706' : 'var(--color-accent-primary)' }}
            >
              <span className="text-xs font-bold text-white">{step}</span>
            </div>
            {/* File icon */}
            <div
              className="flex-shrink-0 w-9 h-9 rounded-xl flex items-center justify-center"
              style={{ background: isPlaceholder ? 'rgba(217,119,6,0.1)' : 'rgba(var(--color-accent-primary-rgb,0,85,164),0.1)' }}
            >
              <FileText className="h-4 w-4" style={{ color: isPlaceholder ? '#d97706' : 'var(--color-accent-primary)' }} />
            </div>
            {/* File info */}
            <div className="min-w-0 flex-1">
              <p
                className="text-xs font-semibold uppercase tracking-wide mb-0.5"
                style={{ color: isPlaceholder ? '#d97706' : 'var(--color-accent-primary)' }}
              >
                {label}
              </p>
              <p className="text-sm font-medium text-[var(--color-text-heading)] truncate">
                {file.name}
              </p>
              <p className="text-xs text-[var(--color-text-muted)]">
                {isPlaceholder ? 'File not available — drop to replace' : formatFileSize(file.size)}
              </p>
            </div>
            {/* Preview — hidden for placeholders */}
            {!isPlaceholder && (
              <button
                onClick={openPreview}
                className="flex-shrink-0 w-7 h-7 rounded-lg flex items-center justify-center text-[var(--color-text-muted)] hover:text-[var(--color-accent-primary)] hover:bg-[var(--color-accent-primary)]/10 transition-all cursor-pointer"
                title="Preview file"
              >
                <Eye className="h-3.5 w-3.5" />
              </button>
            )}
            {/* Download — hidden for placeholders */}
            {!isPlaceholder && (
              <a
                href={`data:${file.mimeType};base64,${file.data}`}
                download={file.name}
                className="flex-shrink-0 w-7 h-7 rounded-lg flex items-center justify-center text-[var(--color-text-muted)] hover:text-[var(--color-accent-primary)] hover:bg-[var(--color-accent-primary)]/10 transition-all cursor-pointer"
                title="Download file"
              >
                <Download className="h-3.5 w-3.5" />
              </a>
            )}
            {/* Remove */}
            {!disabled && (
              <button
                onClick={onRemove}
                className="flex-shrink-0 w-7 h-7 rounded-lg flex items-center justify-center text-[var(--color-text-muted)] hover:text-[var(--color-error)] hover:bg-[var(--color-error)]/10 transition-all cursor-pointer"
                title="Remove file"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            )}
          </div>
        </div>

        {/* Preview modal */}
        {previewOpen && (
          <div
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
            onClick={() => setPreviewOpen(false)}
          >
            <div
              className="relative flex flex-col rounded-2xl shadow-2xl overflow-hidden"
              style={{
                width: '90vw',
                maxWidth: '1300px',
                height: '92vh',
                background: 'var(--color-background)',
              }}
              onClick={e => e.stopPropagation()}
            >
              {/* Modal header */}
              <div
                className="flex items-center justify-between px-5 py-3 border-b"
                style={{ borderColor: 'var(--color-border)' }}
              >
                <span className="text-sm font-semibold truncate" style={{ color: 'var(--color-text-heading)' }}>
                  {label} — {file.name}
                </span>
                <button
                  onClick={() => setPreviewOpen(false)}
                  className="w-7 h-7 rounded-lg flex items-center justify-center transition-all cursor-pointer"
                  style={{ color: 'var(--color-text-muted)' }}
                  onMouseEnter={e => { e.currentTarget.style.color = 'var(--color-error)'; e.currentTarget.style.background = 'var(--color-error)/10'; }}
                  onMouseLeave={e => { e.currentTarget.style.color = 'var(--color-text-muted)'; e.currentTarget.style.background = 'transparent'; }}
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
              {/* Modal content */}
              <div className="flex-1 overflow-hidden">
                {file.mimeType === 'application/pdf' ? (
                  <iframe
                    src={`data:application/pdf;base64,${file.data}`}
                    className="w-full h-full border-0"
                    title={file.name}
                  />
                ) : file.mimeType.startsWith('image/') ? (
                  <div className="w-full h-full flex items-center justify-center p-4 overflow-auto">
                    <img
                      src={`data:${file.mimeType};base64,${file.data}`}
                      alt={file.name}
                      className="max-w-full max-h-full object-contain rounded-lg"
                    />
                  </div>
                ) : isDocxType ? (
                  <ExactDocxPreview bytes={file.bytes} filename={file.name} />
                ) : previewLoading ? (
                  <div className="w-full h-full flex flex-col items-center justify-center gap-3" style={{ color: 'var(--color-text-muted)' }}>
                    <Loader2 className="h-8 w-8 animate-spin" style={{ color: 'var(--color-accent-primary)' }} />
                    <p className="text-sm">Generating preview…</p>
                  </div>
                ) : previewHtml ? (
                  <iframe
                    srcDoc={previewHtml}
                    className="w-full h-full border-0"
                    title={file.name}
                    sandbox="allow-same-origin"
                  />
                ) : (
                  <div className="w-full h-full flex flex-col items-center justify-center gap-4" style={{ color: 'var(--color-text-muted)' }}>
                    <FileText className="h-16 w-16 opacity-30" />
                    <p className="text-sm">Preview unavailable</p>
                    <p className="text-xs opacity-60">{file.name}</p>
                  </div>
                )}
              </div>
            </div>
          </div>
        )}
      </>
    );
  }

  // Empty state
  return (
    <div
      className={`flex-1 min-w-0 rounded-2xl border-2 border-dashed p-6 cursor-pointer group transition-all duration-200 ${
        isDragOver
          ? 'border-[var(--color-accent-primary)] bg-[var(--color-accent-primary)]/8 scale-[1.01]'
          : 'border-[var(--color-border)]/50 hover:border-[var(--color-accent-primary)]/60 hover:bg-[var(--color-accent-primary)]/3'
      } ${disabled ? 'opacity-40 pointer-events-none' : ''}`}
      onDrop={onDrop}
      onDragOver={(e) => { e.preventDefault(); setIsDragOver(true); }}
      onDragLeave={() => setIsDragOver(false)}
      onClick={() => !disabled && inputRef.current?.click()}
    >
      <input
        ref={inputRef}
        type="file"
        accept={FILE_ACCEPT}
        className="hidden"
        onChange={(e) => { const f = e.target.files?.[0]; if (f) handleFile(f); e.target.value = ''; }}
      />
      <div className="flex flex-col items-center gap-3 text-center">
        {/* Step circle + icon */}
        <div className="relative">
          <div className={`w-14 h-14 rounded-2xl flex items-center justify-center transition-colors ${
            isDragOver ? 'bg-[var(--color-accent-primary)]/15' : 'bg-[var(--color-muted)]/60 group-hover:bg-[var(--color-accent-primary)]/10'
          }`}>
            <Upload className={`h-6 w-6 transition-colors ${
              isDragOver ? 'text-[var(--color-accent-primary)]' : 'text-[var(--color-text-muted)] group-hover:text-[var(--color-accent-primary)]'
            }`} />
          </div>
          <span className="absolute -top-1.5 -right-1.5 w-5 h-5 rounded-full bg-[var(--color-accent-primary)] flex items-center justify-center text-[10px] font-bold text-white">
            {step}
          </span>
        </div>
        <div>
          <p className="text-sm font-semibold text-[var(--color-text-heading)]">{label}</p>
          <p className="text-xs text-[var(--color-text-muted)] mt-0.5">Drop here or click to browse</p>
          <p className="text-xs text-[var(--color-text-muted)]/70 mt-0.5">PDF, DOCX, PPTX, Excel, image · max 20 MB</p>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// FeedbackSection — standalone card shown between upload card and results
// ---------------------------------------------------------------------------

interface FeedbackProps {
  submissionKey: string;
  messageId: number | null;
}


// ---------------------------------------------------------------------------
// Structured (JSON) diff helpers
// ---------------------------------------------------------------------------

interface DiffItem {
  section?: string;
  page?: string;
  type?: string;
  criticality?: string;
  before?: string;
  after?: string;
  rationale?: string;
}

interface ImagePair {
  status: 'modified' | 'added' | 'removed' | 'orientation_changed';
  old_page: number | null;
  new_page: number | null;
  old_b64: string | null;
  new_b64: string | null;
  index: number;
}

// ---------------------------------------------------------------------------
// Single-document summary — independent of the diff, one cheap LLM call
// ---------------------------------------------------------------------------

interface DocSummaryMeta {
  truncated: boolean;
  durationS: number;
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  costEur: number;
}

interface DocSummaryState {
  text: string;
  error: string;
  loading: boolean;
  noContent: boolean;
  meta: DocSummaryMeta | null;
}

const EMPTY_SUMMARY: DocSummaryState = { text: '', error: '', loading: false, noContent: false, meta: null };

/** Extract complete JSON objects from a partial/streaming JSON array string. */
function parsePartialJsonItems(text: string): DiffItem[] {
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

const CRIT_STYLE: Record<string, { color: string; badge: string }> = {
  critical: { color: '#dc2626', badge: '#fef2f2' },
  high:     { color: '#ea580c', badge: '#fff7ed' },
  medium:   { color: '#d97706', badge: '#fffbeb' },
  low:      { color: '#16a34a', badge: '#f0fdf4' },
  minor:    { color: '#16a34a', badge: '#f0fdf4' },
};

const IMAGE_KEYWORDS = ['image', 'figure', 'visual', 'photo', 'diagram', 'illustration', 'screenshot', 'picture'];

function JsonDiffTable({
  items,
  isStreaming,
  accentColor,
  fileType,
  imageContext = [],
}: {
  items: DiffItem[];
  isStreaming: boolean;
  accentColor: string;
  fileType?: string;
  imageContext?: ImagePair[];
}) {
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const [lightboxPair, setLightboxPair] = useState<ImagePair | null>(null);
  // Low-criticality rows are genuine changes (not model noise — the analysis
  // prompt deliberately reports every trivial edit rather than risk missing
  // a real one), but they drown out the changes that need action. Hide them
  // by default; nothing is discarded, just collapsed behind a toggle.
  const [hideLow, setHideLow] = useState(true);

  const hasImages = imageContext.length > 0;
  const lowCount = items.filter(it => (it.criticality || '').toLowerCase() === 'low').length;

  useEffect(() => {
    if (!isStreaming) return;
    const el = scrollContainerRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (distanceFromBottom < 80) el.scrollTop = el.scrollHeight;
  }, [items.length, isStreaming]);

  useEffect(() => {
    if (!lightboxPair) return;
    const onKeyDown = (e: KeyboardEvent) => { if (e.key === 'Escape') setLightboxPair(null); };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [lightboxPair]);

  if (items.length === 0) {
    if (isStreaming) {
      return (
        <div className="flex items-center gap-2 text-[var(--color-text-muted)] p-5">
          <Loader2 className="h-4 w-4 animate-spin" style={{ color: accentColor }} />
          <span className="italic text-sm">Generating structured analysis…</span>
        </div>
      );
    }
    return (
      <div className="px-5 py-4 text-sm italic text-[var(--color-text-muted)]">
        No significant changes detected.
      </div>
    );
  }

  return (
    <div>
      {!isStreaming && lowCount > 0 && (
        <div className="flex items-center justify-end px-5 pt-3 pb-1">
          <button
            onClick={() => setHideLow(v => !v)}
            className="text-xs text-[var(--color-text-muted)] hover:text-[var(--color-accent-primary)] underline underline-offset-2"
          >
            {hideLow ? `Show ${lowCount} more (Low criticality)` : 'Hide Low-criticality rows'}
          </button>
        </div>
      )}
      {isStreaming && (
        <div className="flex items-center gap-2 px-5 pt-3 pb-1 text-xs text-[var(--color-text-muted)]">
          <Loader2 className="h-3 w-3 animate-spin" style={{ color: accentColor }} />
          <span>{items.length} item{items.length !== 1 ? 's' : ''} found…</span>
        </div>
      )}
      <div ref={scrollContainerRef} className="overflow-auto max-h-[650px] px-5 py-3">
        <table className="w-full text-xs border-collapse" style={{ minWidth: hasImages ? 820 : 700 }}>
          <thead>
            <tr style={{ background: 'var(--color-muted)' }}>
              {[
                ...(fileType === 'pptx'
                  ? ['Section', 'Slide', 'Type', 'Criticality', 'Before', 'After', 'Rationale']
                  : fileType === 'xml'
                    ? ['Section', 'No.', 'Type', 'Criticality', 'Before', 'After', 'Rationale']
                    : fileType === 'excel'
                      ? ['Section', 'Type', 'Criticality', 'Before', 'After', 'Rationale']
                      : ['Section', 'Page', 'Type', 'Criticality', 'Before', 'After', 'Rationale']),
                ...(hasImages ? ['Image'] : []),
              ].map(h => (
                <th
                  key={h}
                  className="px-2 py-1.5 text-left font-semibold whitespace-nowrap"
                  style={{ color: 'var(--color-text-heading)', borderBottom: '1px solid var(--color-border)' }}
                >
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {(() => {
              let seqIdx = 0;
              return items.map((item, i) => {
                const critKey = (item.criticality || '').toLowerCase();
                const crit = CRIT_STYLE[critKey] ?? { color: 'var(--color-text-body)', badge: 'transparent' };
                const showPage = fileType !== 'excel';

                // Match visual-change rows to image pairs sequentially in LLM output order.
                // Page-number matching is unreliable: the LLM may write old_page, new_page,
                // or an arbitrary value — sequential order is consistent with how the LLM
                // processed the images (orientation → modified → removed → added).
                let imgPair: ImagePair | null = null;
                if (hasImages) {
                  const typeStr = (item.type || '').toLowerCase();
                  const isImgRow = IMAGE_KEYWORDS.some(kw => typeStr.includes(kw));
                  if (isImgRow && seqIdx < imageContext.length) {
                    imgPair = imageContext[seqIdx++];
                  }
                }

                // seqIdx must advance over every item (computed above) so image
                // pairing stays correct regardless of what's hidden below.
                if (hideLow && critKey === 'low') {
                  return null;
                }

                return (
                  <tr key={i} style={{ background: i % 2 === 0 ? 'transparent' : 'var(--color-muted)' }}>
                    <td className="px-2 py-1.5 align-top font-medium" style={{ borderBottom: '1px solid var(--color-border)', maxWidth: 140, wordBreak: 'break-word' }}>
                      {item.section || '—'}
                    </td>
                    {showPage && (
                      <td className="px-2 py-1.5 align-top whitespace-nowrap text-center" style={{ borderBottom: '1px solid var(--color-border)' }}>
                        {item.page || '—'}
                      </td>
                    )}
                    <td className="px-2 py-1.5 align-top whitespace-nowrap" style={{ borderBottom: '1px solid var(--color-border)' }}>
                      {item.type || '—'}
                    </td>
                    <td className="px-2 py-1.5 align-top" style={{ borderBottom: '1px solid var(--color-border)' }}>
                      {item.criticality ? (
                        <span className="px-1.5 py-0.5 rounded text-xs font-semibold" style={{ color: crit.color, background: crit.badge }}>
                          {item.criticality}
                        </span>
                      ) : '—'}
                    </td>
                    <td className="px-2 py-1.5 align-top" style={{ borderBottom: '1px solid var(--color-border)', maxWidth: 200, wordBreak: 'break-word' }}>
                      {item.before || '—'}
                    </td>
                    <td className="px-2 py-1.5 align-top" style={{ borderBottom: '1px solid var(--color-border)', maxWidth: 200, wordBreak: 'break-word' }}>
                      {item.after || '—'}
                    </td>
                    <td className="px-2 py-1.5 align-top" style={{ borderBottom: '1px solid var(--color-border)', maxWidth: 220, wordBreak: 'break-word' }}>
                      {item.rationale || '—'}
                    </td>
                    {hasImages && (
                      <td className="px-2 py-1 align-middle" style={{ borderBottom: '1px solid var(--color-border)', width: 120 }}>
                        {imgPair ? (
                          <div className="flex gap-1 items-center justify-center">
                            {imgPair.old_b64 && (
                              <img
                                src={`data:image/jpeg;base64,${imgPair.old_b64}`}
                                alt="before"
                                title="Click to enlarge — Before"
                                className="max-h-14 max-w-[54px] object-contain rounded border border-[var(--color-border)] cursor-pointer opacity-90 hover:opacity-100"
                                onClick={() => setLightboxPair(imgPair)}
                              />
                            )}
                            {imgPair.new_b64 && (
                              <img
                                src={`data:image/jpeg;base64,${imgPair.new_b64}`}
                                alt="after"
                                title="Click to enlarge — After"
                                className="max-h-14 max-w-[54px] object-contain rounded border border-[var(--color-border)] cursor-pointer opacity-90 hover:opacity-100"
                                onClick={() => setLightboxPair(imgPair)}
                              />
                            )}
                          </div>
                        ) : '—'}
                      </td>
                    )}
                  </tr>
                );
              });
            })()}
          </tbody>
        </table>
        <div ref={bottomRef} />
      </div>

      {lightboxPair && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-6"
          onClick={() => setLightboxPair(null)}
        >
          <div
            className="relative flex flex-col rounded-2xl shadow-2xl overflow-hidden"
            style={{ maxWidth: '95vw', maxHeight: '92vh', background: 'var(--color-background)' }}
            onClick={e => e.stopPropagation()}
          >
            <div
              className="flex items-center justify-between px-4 py-2.5 border-b"
              style={{ borderColor: 'var(--color-border)' }}
            >
              <span className="text-sm font-semibold" style={{ color: 'var(--color-text-heading)' }}>
                Image comparison
              </span>
              <button
                onClick={() => setLightboxPair(null)}
                className="w-7 h-7 rounded-lg flex items-center justify-center transition-all cursor-pointer"
                style={{ color: 'var(--color-text-muted)' }}
                onMouseEnter={e => { e.currentTarget.style.color = 'var(--color-error)'; e.currentTarget.style.background = 'var(--color-error)/10'; }}
                onMouseLeave={e => { e.currentTarget.style.color = 'var(--color-text-muted)'; e.currentTarget.style.background = 'transparent'; }}
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="flex-1 overflow-auto flex flex-wrap items-start justify-center gap-4 p-4">
              {lightboxPair.old_b64 && (
                <div className="flex flex-col items-center gap-1.5">
                  <span className="text-xs font-semibold uppercase tracking-wide" style={{ color: 'var(--color-text-muted)' }}>
                    Before{lightboxPair.old_page ? ` — page ${lightboxPair.old_page}` : ''}
                  </span>
                  <img
                    src={`data:image/jpeg;base64,${lightboxPair.old_b64}`}
                    alt="before"
                    className="object-contain rounded-lg border border-[var(--color-border)]"
                    style={{ maxWidth: '44vw', maxHeight: '78vh' }}
                  />
                </div>
              )}
              {lightboxPair.new_b64 && (
                <div className="flex flex-col items-center gap-1.5">
                  <span className="text-xs font-semibold uppercase tracking-wide" style={{ color: 'var(--color-text-muted)' }}>
                    After{lightboxPair.new_page ? ` — page ${lightboxPair.new_page}` : ''}
                  </span>
                  <img
                    src={`data:image/jpeg;base64,${lightboxPair.new_b64}`}
                    alt="after"
                    className="object-contain rounded-lg border border-[var(--color-border)]"
                    style={{ maxWidth: '44vw', maxHeight: '78vh' }}
                  />
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// StructuredResultCard — same as ResultCard but renders a JsonDiffTable + Excel export
// ---------------------------------------------------------------------------

function StructuredResultCard({
  items,
  isStreaming,
  accentColor,
  fileType,
  imageContext = [],
  onExportExcel,
  feedbackProps,
}: {
  items: DiffItem[];
  isStreaming: boolean;
  accentColor: string;
  fileType?: string;
  imageContext?: ImagePair[];
  onExportExcel: () => void;
  feedbackProps?: FeedbackProps;
}) {
  const [vote, setVote] = useState<'up' | 'down' | null>(null);
  const [comment, setComment] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [feedbackDone, setFeedbackDone] = useState(false);

  useEffect(() => {
    if (!feedbackProps) return;
    if (isFeedbackSubmitted(feedbackProps.submissionKey)) {
      setFeedbackDone(true);
    } else {
      setFeedbackDone(false);
      setVote(null);
      setComment('');
    }
  }, [feedbackProps?.submissionKey]);

  const submitFeedback = async () => {
    if (!vote || submitting || !feedbackProps) return;
    setSubmitting(true);
    try {
      await fetch('/api/feedback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ vote, comment: comment.trim() || null, message_id: feedbackProps.messageId ?? null }),
      });
      markFeedbackSubmitted(feedbackProps.submissionKey);
    } catch { /* best-effort */ }
    finally { setSubmitting(false); setFeedbackDone(true); }
  };

  return (
    <div className="rounded-2xl border border-[var(--color-border)]/40 bg-[var(--color-background)] shadow-sm overflow-hidden">
      <div className="h-0.5 w-full" style={{ background: accentColor }} />
      <div className="flex items-center justify-between px-5 py-4 border-b border-[var(--color-border)]/30 bg-[var(--color-bg-secondary)]/40 gap-3">
        <div className="flex items-center gap-2.5 min-w-0 flex-1">
          <div className="flex-shrink-0 w-7 h-7 rounded-lg flex items-center justify-center" style={{ background: `${accentColor}18` }}>
            <FileText className="h-3.5 w-3.5" style={{ color: accentColor }} />
          </div>
          <span className="text-sm font-semibold text-[var(--color-text-heading)] truncate">Change Table</span>
          {isStreaming && (
            <span className="flex-shrink-0 flex items-center gap-1.5 text-xs text-[var(--color-text-muted)]">
              <Loader2 className="h-3 w-3 animate-spin" style={{ color: accentColor }} />
              Generating…
            </span>
          )}
        </div>

        <div className="flex-shrink-0 flex items-center gap-3">
          {!isStreaming && items.length > 0 && (
            <span className="text-xs text-[var(--color-text-muted)]">{items.length} change{items.length !== 1 ? 's' : ''}</span>
          )}

          {/* Inline feedback — thumbs in header */}
          {feedbackProps && (
            feedbackDone ? (
              <div className="flex items-center gap-1.5" style={{ color: 'var(--color-success)' }}>
                <CheckCircle2 className="h-3.5 w-3.5" />
                <span className="text-xs font-medium">Sent</span>
              </div>
            ) : (
              <div className="flex items-center gap-1.5">
                <span className="text-xs font-medium" style={{ color: 'var(--color-text-muted)' }}>Feedback</span>
                <button
                  onClick={() => setVote(v => v === 'up' ? null : 'up')}
                  title="Helpful"
                  className="w-7 h-7 rounded-lg flex items-center justify-center border transition-all cursor-pointer"
                  style={{
                    borderColor: vote === 'up' ? '#16a34a' : 'var(--color-border)',
                    color: vote === 'up' ? '#16a34a' : 'var(--color-text-muted)',
                    background: vote === 'up' ? '#16a34a12' : 'transparent',
                  }}
                >
                  <ThumbsUp className="h-3.5 w-3.5" />
                </button>
                <button
                  onClick={() => setVote(v => v === 'down' ? null : 'down')}
                  title="Not helpful"
                  className="w-7 h-7 rounded-lg flex items-center justify-center border transition-all cursor-pointer"
                  style={{
                    borderColor: vote === 'down' ? '#dc2626' : 'var(--color-border)',
                    color: vote === 'down' ? '#dc2626' : 'var(--color-text-muted)',
                    background: vote === 'down' ? '#dc262612' : 'transparent',
                  }}
                >
                  <ThumbsDown className="h-3.5 w-3.5" />
                </button>
              </div>
            )
          )}

          {!isStreaming && items.length > 0 && (
            <button
              onClick={onExportExcel}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-all cursor-pointer"
              style={{ color: accentColor }}
              onMouseEnter={e => (e.currentTarget.style.background = `${accentColor}12`)}
              onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
              title="Export analysis as Excel workbook"
            >
              <Download className="h-3.5 w-3.5" />
              Export Excel
            </button>
          )}
        </div>
      </div>

      {/* Comment bar — slides in after a thumb is selected */}
      {feedbackProps && vote && !feedbackDone && (
        <div
          className="flex items-center gap-2.5 px-5 py-2.5 border-b border-[var(--color-border)]/30"
          style={{ background: 'var(--color-bg-secondary)', opacity: 0.95 }}
        >
          <input
            type="text"
            value={comment}
            onChange={e => setComment(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') submitFeedback(); }}
            placeholder="Add a comment (optional)…"
            maxLength={1000}
            className="flex-1 px-3 py-1.5 rounded-lg border text-sm outline-none transition-colors"
            style={{
              borderColor: 'var(--color-border)',
              background: 'var(--color-background)',
              color: 'var(--color-text-body)',
            }}
            onFocus={e => (e.currentTarget.style.borderColor = 'var(--color-accent-primary)')}
            onBlur={e => (e.currentTarget.style.borderColor = 'var(--color-border)')}
          />
          <button
            onClick={submitFeedback}
            disabled={submitting}
            className="flex-shrink-0 flex items-center gap-1.5 px-3.5 py-1.5 rounded-lg text-xs font-semibold text-white transition-all disabled:opacity-40 cursor-pointer"
            style={{ background: 'linear-gradient(135deg, var(--color-accent-primary) 0%, var(--color-accent-secondary) 100%)' }}
          >
            <Send className="h-3 w-3" />
            {submitting ? 'Sending…' : 'Send'}
          </button>
        </div>
      )}

      <JsonDiffTable items={items} isStreaming={isStreaming} accentColor={accentColor} fileType={fileType} imageContext={imageContext} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// ResultCard
// ---------------------------------------------------------------------------

function ResultCard({
  title,
  icon: Icon,
  accentColor,
  content,
  isStreaming,
  onDownload,
  feedbackProps,
}: {
  title: string;
  icon: React.ElementType;
  accentColor: string;
  content: string;
  isStreaming: boolean;
  onDownload: () => void;
  feedbackProps?: FeedbackProps;
}) {
  const bottomRef = useRef<HTMLDivElement>(null);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const [vote, setVote] = useState<'up' | 'down' | null>(null);
  const [comment, setComment] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [feedbackDone, setFeedbackDone] = useState(false);

  // Auto-scroll only if the user is already at (or near) the bottom.
  // Uses scrollTop directly — no smooth animation that fights manual scrolling.
  useEffect(() => {
    if (!isStreaming) return;
    const el = scrollContainerRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (distanceFromBottom < 60) {
      el.scrollTop = el.scrollHeight;
    }
  }, [content, isStreaming]);

  useEffect(() => {
    if (!feedbackProps) return;
    if (isFeedbackSubmitted(feedbackProps.submissionKey)) {
      setFeedbackDone(true);
    } else {
      setFeedbackDone(false);
      setVote(null);
      setComment('');
    }
  }, [feedbackProps?.submissionKey]);

  const submitFeedback = async () => {
    if (!vote || submitting || !feedbackProps) return;
    setSubmitting(true);
    try {
      await fetch('/api/feedback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ vote, comment: comment.trim() || null, message_id: feedbackProps.messageId ?? null }),
      });
      markFeedbackSubmitted(feedbackProps.submissionKey);
    } catch { /* best-effort */ }
    finally { setSubmitting(false); setFeedbackDone(true); }
  };

  return (
    <div className="rounded-2xl border border-[var(--color-border)]/40 bg-[var(--color-background)] shadow-sm overflow-hidden">
      {/* Colored top accent bar */}
      <div className="h-0.5 w-full" style={{ background: accentColor }} />

      {/* Header */}
      <div className="flex items-center justify-between px-5 py-4 border-b border-[var(--color-border)]/30 bg-[var(--color-bg-secondary)]/40 gap-3">
        <div className="flex items-center gap-2.5 min-w-0 flex-1">
          <div className="flex-shrink-0 w-7 h-7 rounded-lg flex items-center justify-center" style={{ background: `${accentColor}18` }}>
            <Icon className="h-3.5 w-3.5" style={{ color: accentColor }} />
          </div>
          <span className="text-sm font-semibold text-[var(--color-text-heading)] truncate">{title}</span>
          {isStreaming && (
            <span className="flex-shrink-0 flex items-center gap-1.5 text-xs text-[var(--color-text-muted)]">
              <Loader2 className="h-3 w-3 animate-spin" style={{ color: accentColor }} />
              Generating…
            </span>
          )}
        </div>

        <div className="flex-shrink-0 flex items-center gap-3">
          {/* Inline feedback — thumbs in header */}
          {feedbackProps && (
            feedbackDone ? (
              <div className="flex items-center gap-1.5" style={{ color: 'var(--color-success)' }}>
                <CheckCircle2 className="h-3.5 w-3.5" />
                <span className="text-xs font-medium">Sent</span>
              </div>
            ) : (
              <div className="flex items-center gap-1.5">
                <span className="text-xs font-medium" style={{ color: 'var(--color-text-muted)' }}>Feedback</span>
                <button
                  onClick={() => setVote(v => v === 'up' ? null : 'up')}
                  title="Helpful"
                  className="w-7 h-7 rounded-lg flex items-center justify-center border transition-all cursor-pointer"
                  style={{
                    borderColor: vote === 'up' ? '#16a34a' : 'var(--color-border)',
                    color: vote === 'up' ? '#16a34a' : 'var(--color-text-muted)',
                    background: vote === 'up' ? '#16a34a12' : 'transparent',
                  }}
                >
                  <ThumbsUp className="h-3.5 w-3.5" />
                </button>
                <button
                  onClick={() => setVote(v => v === 'down' ? null : 'down')}
                  title="Not helpful"
                  className="w-7 h-7 rounded-lg flex items-center justify-center border transition-all cursor-pointer"
                  style={{
                    borderColor: vote === 'down' ? '#dc2626' : 'var(--color-border)',
                    color: vote === 'down' ? '#dc2626' : 'var(--color-text-muted)',
                    background: vote === 'down' ? '#dc262612' : 'transparent',
                  }}
                >
                  <ThumbsDown className="h-3.5 w-3.5" />
                </button>
              </div>
            )
          )}

          {content && !isStreaming && (
            <button
              onClick={onDownload}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-all cursor-pointer"
              style={{ color: accentColor }}
              onMouseEnter={e => (e.currentTarget.style.background = `${accentColor}12`)}
              onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
              title="Download as PDF"
            >
              <Download className="h-3.5 w-3.5" />
              Download PDF
            </button>
          )}
        </div>
      </div>

      {/* Comment bar — slides in after a thumb is selected */}
      {feedbackProps && vote && !feedbackDone && (
        <div
          className="flex items-center gap-2.5 px-5 py-2.5 border-b border-[var(--color-border)]/30"
          style={{ background: 'var(--color-bg-secondary)', opacity: 0.95 }}
        >
          <input
            type="text"
            value={comment}
            onChange={e => setComment(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') submitFeedback(); }}
            placeholder="Add a comment (optional)…"
            maxLength={1000}
            className="flex-1 px-3 py-1.5 rounded-lg border text-sm outline-none transition-colors"
            style={{
              borderColor: 'var(--color-border)',
              background: 'var(--color-background)',
              color: 'var(--color-text-body)',
            }}
            onFocus={e => (e.currentTarget.style.borderColor = 'var(--color-accent-primary)')}
            onBlur={e => (e.currentTarget.style.borderColor = 'var(--color-border)')}
          />
          <button
            onClick={submitFeedback}
            disabled={submitting}
            className="flex-shrink-0 flex items-center gap-1.5 px-3.5 py-1.5 rounded-lg text-xs font-semibold text-white transition-all disabled:opacity-40 cursor-pointer"
            style={{ background: 'linear-gradient(135deg, var(--color-accent-primary) 0%, var(--color-accent-secondary) 100%)' }}
          >
            <Send className="h-3 w-3" />
            {submitting ? 'Sending…' : 'Send'}
          </button>
        </div>
      )}

      {/* Content */}
      <div ref={scrollContainerRef} className="p-5 max-h-[650px] overflow-y-auto text-sm leading-relaxed">
        {content ? (
          <MarkdownRenderer content={content} />
        ) : (
          <div className="flex items-center gap-2 text-[var(--color-text-muted)]">
            <Loader2 className="h-4 w-4 animate-spin" style={{ color: accentColor }} />
            <span className="italic text-sm">Waiting for response…</span>
          </div>
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// DocSummaryCard — single-document summary (independent of the diff)
// ---------------------------------------------------------------------------

function DocSummaryCard({
  title,
  state,
  accentColor,
  onDownload,
}: {
  title: string;
  state: DocSummaryState;
  accentColor: string;
  onDownload: () => void;
}) {
  const { text, error, loading, noContent, meta } = state;
  return (
    <div className="rounded-2xl border border-[var(--color-border)]/40 bg-[var(--color-background)] shadow-sm overflow-hidden">
      <div className="h-0.5 w-full" style={{ background: accentColor }} />

      <div className="flex items-center justify-between px-5 py-4 border-b border-[var(--color-border)]/30 bg-[var(--color-bg-secondary)]/40">
        <div className="flex items-center gap-2.5">
          <div className="w-7 h-7 rounded-lg flex items-center justify-center" style={{ background: `${accentColor}18` }}>
            <FileText className="h-3.5 w-3.5" style={{ color: accentColor }} />
          </div>
          <span className="text-sm font-semibold text-[var(--color-text-heading)]">{title}</span>
          {loading && (
            <span className="flex items-center gap-1.5 text-xs text-[var(--color-text-muted)]">
              <Loader2 className="h-3 w-3 animate-spin" style={{ color: accentColor }} />
              Summarizing…
            </span>
          )}
        </div>
        {!loading && text && (
          <button
            onClick={onDownload}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-all cursor-pointer"
            style={{ color: accentColor }}
            onMouseEnter={e => (e.currentTarget.style.background = `${accentColor}12`)}
            onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
            title="Download as PDF"
          >
            <Download className="h-3.5 w-3.5" />
            Download PDF
          </button>
        )}
      </div>

      {meta?.truncated && (
        <div className="px-5 py-2 text-xs" style={{ color: '#d97706', background: '#fffbeb' }}>
          ⚠️ Document text was truncated before summarizing — result may be partial.
        </div>
      )}

      <div className="p-5 max-h-[650px] overflow-y-auto text-sm leading-relaxed">
        {loading && !text ? (
          <div className="flex items-center gap-2 text-[var(--color-text-muted)]">
            <Loader2 className="h-4 w-4 animate-spin" style={{ color: accentColor }} />
            <span className="italic text-sm">Summarizing…</span>
          </div>
        ) : error ? (
          <p className="text-sm" style={{ color: 'var(--color-error)' }}>{error}</p>
        ) : text ? (
          <MarkdownRenderer content={text} />
        ) : noContent ? (
          <p className="text-sm italic text-[var(--color-text-muted)]">
            No extractable text found in this document (e.g. a scanned page with no text layer) — nothing to summarize.
          </p>
        ) : (
          <p className="text-sm italic text-[var(--color-text-muted)]">No summary yet.</p>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main CompareView
// ---------------------------------------------------------------------------

export function CompareView() {
  const [oldPdf, setOldPdf] = useState<DocFile | null>(null);
  const [newPdf, setNewPdf] = useState<DocFile | null>(null);
  const [analysis, setAnalysis] = useState('');
  const [impact, setImpact] = useState<ImpactResult | null>(null);
  const [impactError, setImpactError] = useState('');
  const [isAnalyzing, setIsAnalyzing] = useState(false);
  const [isImpacting, setIsImpacting] = useState(false);
  const [_sessionPath, setSessionPath] = useState('');
  const [error, setError] = useState('');
  const [volumeConfigured, setVolumeConfigured] = useState(false);
  const [impactConfigured, setImpactConfigured] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [showWhatsNew, setShowWhatsNew] = useState(false);
  const [messageId, setMessageId] = useState<number | null>(null);
  const [oldFileHash, setOldFileHash] = useState('');
  const [newFileHash, setNewFileHash] = useState('');
  const [currentFileType, setCurrentFileType] = useState('');
  const [parsedJsonItems, setParsedJsonItems] = useState<DiffItem[]>([]);
  const [imageContext, setImageContext] = useState<ImagePair[]>([]);
  // Change Summary (standard) track — runs alongside Change Table (structured), no selector
  const [analysisStd, setAnalysisStd] = useState('');
  const [isAnalyzingStd, setIsAnalyzingStd] = useState(false);
  const [messageIdStd, setMessageIdStd] = useState<number | null>(null);
  // Which processor versions the current file type actually exposes (from processors.yml)
  const [availableVersions, setAvailableVersions] = useState<ProcessorVersion[]>([]);
  // Single-document summary — independent of the diff, keyed by which file
  const [summaries, setSummaries] = useState<{ old: DocSummaryState; new: DocSummaryState }>({ old: EMPTY_SUMMARY, new: EMPTY_SUMMARY });
  // Which summary tab is shown when both A and B are active — always full-width, one at a time.
  const [activeSummaryTab, setActiveSummaryTab] = useState<'old' | 'new'>('old');
  // Which main-analysis tab is shown when both Change Summary and Change Table are active —
  // same reasoning as activeSummaryTab: side-by-side halves each card's width and reads as cramped.
  const [activeAnalysisTab, setActiveAnalysisTab] = useState<'summary' | 'table'>('summary');
  // Impact search can run off a Compare analysis, or off a hand-typed change description
  // (an email, a general idea) when the user doesn't have both documents on hand.
  const [impactMode, setImpactMode] = useState<'compare' | 'manual'>(
    () => (localStorage.getItem(LS_IMPACT_MODE) === 'manual' ? 'manual' : 'compare'),
  );
  const [manualChangesText, setManualChangesText] = useState(() => localStorage.getItem(LS_IMPACT_MANUAL_TEXT) || '');
  // Manual mode's UI is hidden (IMPACT_MANUAL_MODE_ENABLED) — force 'compare' everywhere it's read,
  // even if a stale 'manual' value lingers in localStorage from before the toggle was hidden.
  const effectiveImpactMode: 'compare' | 'manual' = IMPACT_MANUAL_MODE_ENABLED ? impactMode : 'compare';

  // Abort in-flight analysis when component unmounts (tab switch)
  const abortControllerRef = useRef<AbortController | null>(null);
  const abortControllerRefStd = useRef<AbortController | null>(null);
  useEffect(() => () => { abortControllerRef.current?.abort(); abortControllerRefStd.current?.abort(); }, []);

  useEffect(() => {
    const old = loadPdfFromStorage(LS_OLD_PDF);
    const nw = loadPdfFromStorage(LS_NEW_PDF);
    if (old) setOldPdf(old);
    if (nw) setNewPdf(nw);
    const storedAnalysis = localStorage.getItem(LS_ANALYSIS) || '';
    setAnalysis(storedAnalysis);
    setAnalysisStd(localStorage.getItem(LS_ANALYSIS_STD) || '');
    const storedMidStd = localStorage.getItem(LS_MESSAGE_ID_STD);
    if (storedMidStd) setMessageIdStd(Number(storedMidStd));
    try {
      const storedImpact = localStorage.getItem(LS_IMPACT);
      if (storedImpact) setImpact(JSON.parse(storedImpact) as ImpactResult);
    } catch { /* ignore malformed cache */ }
    setSessionPath(localStorage.getItem(LS_SESSION_PATH) || '');
    const storedMid = localStorage.getItem(LS_MESSAGE_ID);
    if (storedMid) setMessageId(Number(storedMid));
    setOldFileHash(localStorage.getItem(LS_OLD_HASH) || '');
    setNewFileHash(localStorage.getItem(LS_NEW_HASH) || '');
    if (storedAnalysis) {
      setParsedJsonItems(parsePartialJsonItems(storedAnalysis));
    }
  }, []);

  useEffect(() => {
    getAppConfig().then((cfg) => {
      const vp = cfg.compare?.volume_path || '';
      setVolumeConfigured(!!vp && !vp.startsWith('TODO'));
      const idx = (cfg.compare?.impact_index || '').trim();
      // A real Vector Search index name is alphanumeric + dots/hyphens/underscores, no spaces.
      // Treat anything else (empty, TODO, --, -, contains spaces, too short) as not configured.
      const looksReal = idx.length >= 3 && !idx.startsWith('TODO') && !/\s/.test(idx) && !/^-+$/.test(idx);
      setImpactConfigured(looksReal);
    });
  }, []);

  // Load available processor versions whenever the primary file changes.
  // File type is resolved from processors.yml extensions — processors.yml is the single source of truth.
  useEffect(() => {
    const filename = oldPdf?.name || newPdf?.name;
    if (!filename) {
      setAvailableVersions([]);
      return;
    }
    const ext = filename.includes('.') ? '.' + filename.split('.').pop()!.toLowerCase() : '';
    getProcessorsConfig().then((cfg) => {
      const entry = Object.values(cfg.file_types ?? {}).find(ft =>
        (ft.extensions ?? []).includes(ext)
      );
      const versions: ProcessorVersion[] = (entry?.versions ?? []).filter(v => !v.hidden);
      setAvailableVersions(versions);
    });
  }, [oldPdf?.name, newPdf?.name]);

  const clearAnalysisState = () => {
    setAnalysis('');
    setAnalysisStd('');
    setImpact(null);
    setImpactError('');
    setParsedJsonItems([]);
    setMessageId(null);
    setMessageIdStd(null);
    setError('');
    localStorage.removeItem(LS_ANALYSIS);
    localStorage.removeItem(LS_ANALYSIS_STD);
    localStorage.removeItem(LS_IMPACT);
    localStorage.removeItem(LS_PROCESSING_METHOD);
    localStorage.removeItem(LS_MESSAGE_ID);
    localStorage.removeItem(LS_MESSAGE_ID_STD);
    localStorage.removeItem(LS_OLD_HASH);
    localStorage.removeItem(LS_NEW_HASH);
  };

  const setOldPdfAndPersist = (f: DocFile | null) => { setOldPdf(f); savePdfToStorage(LS_OLD_PDF, f); };
  const setNewPdfAndPersist = (f: DocFile | null) => { setNewPdf(f); savePdfToStorage(LS_NEW_PDF, f); };

  const setManualChangesTextAndPersist = (text: string) => {
    setManualChangesText(text);
    localStorage.setItem(LS_IMPACT_MANUAL_TEXT, text);
  };

  const switchImpactMode = (mode: 'compare' | 'manual') => {
    setImpactMode(mode);
    localStorage.setItem(LS_IMPACT_MODE, mode);
    setImpact(null);
    setImpactError('');
    localStorage.removeItem(LS_IMPACT);
  };

  const clearAll = () => {
    setOldPdfAndPersist(null);
    setNewPdfAndPersist(null);
    setAnalysis('');
    setAnalysisStd('');
    setImpact(null);
    setImpactError('');
    setSummaries({ old: EMPTY_SUMMARY, new: EMPTY_SUMMARY });
    setSessionPath('');
    setMessageId(null);
    setMessageIdStd(null);
    setOldFileHash('');
    setNewFileHash('');
    setError('');
    setManualChangesTextAndPersist('');
    localStorage.removeItem(LS_ANALYSIS);
    localStorage.removeItem(LS_ANALYSIS_STD);
    localStorage.removeItem(LS_IMPACT);
    localStorage.removeItem(LS_SESSION_PATH);
    localStorage.removeItem(LS_MESSAGE_ID);
    localStorage.removeItem(LS_MESSAGE_ID_STD);
    localStorage.removeItem(LS_OLD_HASH);
    localStorage.removeItem(LS_NEW_HASH);
  };

  const handleInvalidFile = (msg: string) => { toast.error(msg); setError(msg); };

  // -------------------------------------------------------------------------
  // Auto-save helpers
  // -------------------------------------------------------------------------

  const autoSavePdfs = async (old: DocFile, nw: DocFile): Promise<string> => {
    try {
      const form = new FormData();
      form.append('old_file', new Blob([old.bytes], { type: old.mimeType }), old.name);
      form.append('new_file', new Blob([nw.bytes], { type: nw.mimeType }), nw.name);
      const res = await fetch('/api/compare/save', { method: 'POST', body: form });
      const data = await res.json();
      if (data.session_path) { localStorage.setItem(LS_SESSION_PATH, data.session_path); return data.session_path; }
      if (data.error) toast.warning(`Auto-save skipped: ${data.error}`);
    } catch (e) {
      toast.warning(`Auto-save unavailable: ${e instanceof Error ? e.message : String(e)}`);
    }
    return '';
  };

  const autoSaveResult = async (path: string, filename: string, content: string) => {
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
      toast.warning(`Could not save ${filename}: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const autoSaveExcel = (path: string, jsonText: string, fileType: string, imagePairs?: ImagePair[]) => {
    if (!path || !jsonText.trim()) return;
    const form = new FormData();
    form.append('session_path', path);
    form.append('json_text', jsonText);
    form.append('file_type', fileType);
    form.append('filename', 'analysis');
    if (imagePairs && imagePairs.length > 0) {
      form.append('image_pairs_json', JSON.stringify(imagePairs));
    }
    fetch('/api/compare/save-excel', { method: 'POST', body: form })
      .then(r => r.json())
      .then(d => { if (d.error) toast.warning(`Could not save Excel: ${d.error}`); })
      .catch(() => { /* best-effort — not critical */ });
  };

  const autoSavePdf = (path: string, markdownText: string, title: string) => {
    if (!path || !markdownText.trim()) return;
    const form = new FormData();
    form.append('session_path', path);
    form.append('markdown_text', markdownText);
    form.append('title', title);
    form.append('filename', 'analysis');
    fetch('/api/compare/save-pdf', { method: 'POST', body: form })
      .then(r => r.json())
      .then(d => { if (d.error) toast.warning(`Could not save PDF: ${d.error}`); })
      .catch(() => { /* best-effort — not critical */ });
  };

  // -------------------------------------------------------------------------
  // History helpers
  // -------------------------------------------------------------------------

  const saveToHistory = async (
    oldName: string,
    newName: string,
    analysisText: string,
    impactText: string,
    volumePath: string,
    oldHash = '',
    newHash = '',
    fileType = '',
    processingMethod = '',
    processorVersion = '',
    ttftS = 0,
    generationS = 0,
    usage: { input_tokens?: number; output_tokens?: number; thinking_tokens?: number; total_tokens?: number; cost_eur?: number } = {},
    llmRequestId: number | null = null,
  ): Promise<number | null> => {
    try {
      const res = await fetch('/api/history', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          old_filename: oldName,
          new_filename: newName,
          old_file_hash: oldHash || null,
          new_file_hash: newHash || null,
          analysis_text: analysisText || null,
          impact_text: impactText || null,
          volume_session_path: volumePath || null,
          file_type: fileType || null,
          processing_method: processingMethod || null,
          processor_version: processorVersion || null,
          ttft_s: ttftS || null,
          generation_s: generationS || null,
          input_tokens: usage.input_tokens ?? null,
          output_tokens: usage.output_tokens ?? null,
          thinking_tokens: usage.thinking_tokens ?? null,
          total_tokens: usage.total_tokens ?? null,
          cost_eur: usage.cost_eur ?? null,
          llm_request_id: llmRequestId ?? null,
        }),
      });
      const data = await res.json();
      return typeof data.messages_id === 'number' ? data.messages_id : null;
    } catch {
      return null;
    }
  };

  const handleLoadFromHistory = async (entry: FullComparison) => {
    // A history row is one run of one processor version — route it into the
    // matching track (structured = Change Table, everything else = Change
    // Summary) and leave the other track untouched.
    const method = entry.processing_method || '';
    setError('');
    setOldFileHash('');
    setNewFileHash('');
    localStorage.removeItem(LS_OLD_HASH);
    localStorage.removeItem(LS_NEW_HASH);
    setSessionPath(entry.volume_session_path || '');
    localStorage.setItem(LS_SESSION_PATH, entry.volume_session_path || '');

    if (method === 'structured') {
      setParsedJsonItems(entry.analysis_text ? parsePartialJsonItems(entry.analysis_text) : []);
      setAnalysis(entry.analysis_text || '');
      setMessageId(entry.id);
      localStorage.setItem(LS_PROCESSING_METHOD, method);
      localStorage.setItem(LS_ANALYSIS, entry.analysis_text || '');
      localStorage.setItem(LS_MESSAGE_ID, String(entry.id));
    } else {
      setAnalysisStd(entry.analysis_text || '');
      setMessageIdStd(entry.id);
      localStorage.setItem(LS_ANALYSIS_STD, entry.analysis_text || '');
      localStorage.setItem(LS_MESSAGE_ID_STD, String(entry.id));
    }

    // Show filename placeholders immediately (no bytes — display only, not persisted to localStorage)
    const emptyBytes = new Uint8Array(0) as unknown as Uint8Array<ArrayBuffer>;
    const mimeFor = (name: string) => {
      const ext = name.split('.').pop()?.toLowerCase() || '';
      const m: Record<string, string> = {
        pdf: 'application/pdf',
        docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        doc: 'application/msword',
        pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        ppt: 'application/vnd.ms-powerpoint',
        xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        xls: 'application/vnd.ms-excel',
        jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png',
        gif: 'image/gif', webp: 'image/webp',
      };
      return m[ext] || 'application/octet-stream';
    };
    setOldPdf({ name: entry.old_filename, size: 0, data: '', bytes: emptyBytes, mimeType: mimeFor(entry.old_filename) });
    setNewPdf({ name: entry.new_filename, size: 0, data: '', bytes: emptyBytes, mimeType: mimeFor(entry.new_filename) });

    // Try to restore actual file bytes from volume
    if (entry.volume_session_path) {
      try {
        const params = new URLSearchParams({
          session_path: entry.volume_session_path,
          old_filename: entry.old_filename,
          new_filename: entry.new_filename,
        });
        const res = await fetch(`/api/compare/load?${params}`);
        if (res.ok) {
          const data = await res.json();
          const b64toBytes = (b64: string): Uint8Array<ArrayBuffer> => {
            const bin = atob(b64);
            const buf = new ArrayBuffer(bin.length);
            const bytes = new Uint8Array(buf);
            for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
            return bytes;
          };
          if (data.old) {
            const f: DocFile = { name: data.old.name, size: data.old.size, data: data.old.data, bytes: b64toBytes(data.old.data), mimeType: data.old.mimeType };
            setOldPdfAndPersist(f);
          }
          if (data.new) {
            const f: DocFile = { name: data.new.name, size: data.new.size, data: data.new.data, bytes: b64toBytes(data.new.data), mimeType: data.new.mimeType };
            setNewPdfAndPersist(f);
          }
          if (data.old || data.new) {
            toast.success(`Restored: ${entry.old_filename} → ${entry.new_filename}`);
            return;
          }
        }
      } catch {
        // best-effort — fall through
      }
    }
    toast.success(`Loaded: ${entry.old_filename} → ${entry.new_filename}`);
  };

  // -------------------------------------------------------------------------
  // Analyze
  // -------------------------------------------------------------------------

  const handleAnalyzeStructured = async (forceRefresh = false) => {
    if (!oldPdf || !newPdf) return;
    setError('');
    setAnalysis('');
    setImpact(null);
    setImpactError('');
    setSessionPath('');
    setMessageId(null);
    setParsedJsonItems([]);
    setImageContext([]);
    setIsAnalyzing(true);
    localStorage.removeItem(LS_ANALYSIS);
    localStorage.removeItem(LS_PROCESSING_METHOD);
    localStorage.removeItem(LS_IMPACT);
    localStorage.removeItem(LS_SESSION_PATH);
    localStorage.removeItem(LS_MESSAGE_ID);
    localStorage.removeItem(LS_OLD_HASH);
    localStorage.removeItem(LS_NEW_HASH);

    const [ohash, nhash] = await Promise.all([
      computeFileHash(oldPdf.bytes),
      computeFileHash(newPdf.bytes),
    ]);
    setOldFileHash(ohash);
    setNewFileHash(nhash);
    localStorage.setItem(LS_OLD_HASH, ohash);
    localStorage.setItem(LS_NEW_HASH, nhash);

    let currentSession = '';

    try {
      const form = new FormData();
      form.append('old_file', new Blob([oldPdf.bytes], { type: oldPdf.mimeType }), oldPdf.name);
      form.append('new_file', new Blob([newPdf.bytes], { type: newPdf.mimeType }), newPdf.name);
      form.append('old_file_hash', ohash);
      form.append('new_file_hash', nhash);
      form.append('force_refresh', forceRefresh ? 'true' : 'false');
      form.append('processor_version', 'structured');

      const controller = new AbortController();
      abortControllerRef.current = controller;

      const startTime = Date.now();
      let firstTokenTime: number | null = null;
      const res = await fetch('/api/compare/analyze', { method: 'POST', body: form, signal: controller.signal });
      if (!res.ok) throw new Error(`Server error: ${res.status}`);

      const reader = res.body!.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let accumulated = '';
      let fileType = '';
      let processingMethod = 'structured';
      let isCached = false;
      let cachedMessageId: number | null = null;
      let usageData: { input_tokens?: number; output_tokens?: number; thinking_tokens?: number; total_tokens?: number; cost_eur?: number } = {};

      let cachedSessionPath = '';
      let localImageContext: ImagePair[] = [];
      let llmRequestId: number | null = null;
      let streamDone = false;
      while (!streamDone) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() ?? '';
        let sseError: string | null = null;
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          const data = line.slice(6).trim();
          if (data === '[DONE]') { streamDone = true; break; }
          try {
            const event = JSON.parse(data);
            if (event.type === 'response.output_text.delta') {
              if (firstTokenTime === null) firstTokenTime = Date.now();
              accumulated += event.delta;
              setAnalysis(accumulated);
              localStorage.setItem(LS_ANALYSIS, accumulated);
              setParsedJsonItems(parsePartialJsonItems(accumulated));
            } else if (event.type === 'metadata') {
              fileType = event.file_type || '';
              setCurrentFileType(fileType);
              processingMethod = event.method || 'structured';
              localStorage.setItem(LS_PROCESSING_METHOD, processingMethod);
              llmRequestId = event.llm_request_id ?? null;
              if (event.cached) {
                isCached = true;
                cachedMessageId = event.message_id ?? null;
                cachedSessionPath = event.session_path ?? '';
              }
            } else if (event.type === 'image_context') {
              localImageContext = event.images || [];
              setImageContext(localImageContext);
            } else if (event.type === 'warning') {
              toast.warning(event.detail || 'The report may be incomplete.');
            } else if (event.type === 'usage') {
              usageData = event;
            } else if (event.type === 'error') {
              sseError = event.error ?? 'Unknown server error';
              break;
            }
          } catch { /* skip malformed chunks */ }
        }
        if (sseError) throw new Error(sseError);
      }

      localStorage.setItem(LS_ANALYSIS, accumulated);
      setParsedJsonItems(parsePartialJsonItems(accumulated));

      if (isCached && cachedMessageId !== null) {
        // Result from DB cache — reuse the original session folder
        setMessageId(cachedMessageId);
        localStorage.setItem(LS_MESSAGE_ID, String(cachedMessageId));
        if (cachedSessionPath) {
          setSessionPath(cachedSessionPath);
          localStorage.setItem(LS_SESSION_PATH, cachedSessionPath);
        }
        toast.info('Result retrieved from cache');
      } else {
        const endTime = Date.now();
        const ttftS = firstTokenTime !== null ? (firstTokenTime - startTime) / 1000 : 0;
        const generationS = firstTokenTime !== null ? (endTime - firstTokenTime) / 1000 : (endTime - startTime) / 1000;
        // Create session folder only for fresh (non-cached) analyses
        if (volumeConfigured) {
          currentSession = await autoSavePdfs(oldPdf, newPdf);
          if (currentSession) setSessionPath(currentSession);
        }
        if (currentSession) {
          // Always save the raw analysis text for history restore
          await autoSaveResult(currentSession, 'analysis.md', accumulated);
          autoSaveExcel(currentSession, accumulated, fileType, localImageContext);
        }
        const mid = await saveToHistory(
          oldPdf.name, newPdf.name, accumulated, '', currentSession, ohash, nhash,
          fileType, processingMethod, 'structured', ttftS, generationS, usageData, llmRequestId,
        );
        if (mid !== null) {
          setMessageId(mid);
          localStorage.setItem(LS_MESSAGE_ID, String(mid));
        }
      }
    } catch (err) {
      if (err instanceof Error && err.name === 'AbortError') {
        toast.info('Analysis cancelled — partial result kept.');
        return;
      }
      const raw = err instanceof Error ? err.message : String(err);
      const isNetworkDrop = raw.toLowerCase().includes('failed to fetch')
        || raw.toLowerCase().includes('networkerror')
        || raw.toLowerCase().includes('load failed');
      const msg = isNetworkDrop
        ? 'Connection lost during analysis. Please retry — the server may still be processing.'
        : raw;
      setError(msg);
      toast.error(`Change Table failed: ${msg}`);
    } finally {
      setIsAnalyzing(false);
    }
  };

  const handleAnalyzeStandard = async (forceRefresh = false) => {
    if (!oldPdf || !newPdf) return;
    setError('');
    setAnalysisStd('');
    setImpact(null);
    setImpactError('');
    setSessionPath('');
    setMessageIdStd(null);
    setIsAnalyzingStd(true);
    localStorage.removeItem(LS_ANALYSIS_STD);
    localStorage.removeItem(LS_IMPACT);
    localStorage.removeItem(LS_SESSION_PATH);
    localStorage.removeItem(LS_MESSAGE_ID_STD);
    localStorage.removeItem(LS_OLD_HASH);
    localStorage.removeItem(LS_NEW_HASH);

    const [ohash, nhash] = await Promise.all([
      computeFileHash(oldPdf.bytes),
      computeFileHash(newPdf.bytes),
    ]);
    setOldFileHash(ohash);
    setNewFileHash(nhash);
    localStorage.setItem(LS_OLD_HASH, ohash);
    localStorage.setItem(LS_NEW_HASH, nhash);

    let currentSession = '';

    try {
      const form = new FormData();
      form.append('old_file', new Blob([oldPdf.bytes], { type: oldPdf.mimeType }), oldPdf.name);
      form.append('new_file', new Blob([newPdf.bytes], { type: newPdf.mimeType }), newPdf.name);
      form.append('old_file_hash', ohash);
      form.append('new_file_hash', nhash);
      form.append('force_refresh', forceRefresh ? 'true' : 'false');
      form.append('processor_version', 'standard');

      const controller = new AbortController();
      abortControllerRefStd.current = controller;

      const startTime = Date.now();
      let firstTokenTime: number | null = null;
      const res = await fetch('/api/compare/analyze', { method: 'POST', body: form, signal: controller.signal });
      if (!res.ok) throw new Error(`Server error: ${res.status}`);

      const reader = res.body!.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let accumulated = '';
      let fileType = '';
      let isCached = false;
      let cachedMessageId: number | null = null;
      let usageData: { input_tokens?: number; output_tokens?: number; thinking_tokens?: number; total_tokens?: number; cost_eur?: number } = {};
      let cachedSessionPath = '';
      let llmRequestId: number | null = null;
      let streamDone = false;
      while (!streamDone) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() ?? '';
        let sseError: string | null = null;
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          const data = line.slice(6).trim();
          if (data === '[DONE]') { streamDone = true; break; }
          try {
            const event = JSON.parse(data);
            if (event.type === 'response.output_text.delta') {
              if (firstTokenTime === null) firstTokenTime = Date.now();
              accumulated += event.delta;
              setAnalysisStd(accumulated);
              localStorage.setItem(LS_ANALYSIS_STD, accumulated);
            } else if (event.type === 'metadata') {
              fileType = event.file_type || '';
              setCurrentFileType(fileType);
              llmRequestId = event.llm_request_id ?? null;
              if (event.cached) {
                isCached = true;
                cachedMessageId = event.message_id ?? null;
                cachedSessionPath = event.session_path ?? '';
              }
            } else if (event.type === 'warning') {
              toast.warning(event.detail || 'The report may be incomplete.');
            } else if (event.type === 'usage') {
              usageData = event;
            } else if (event.type === 'error') {
              sseError = event.error ?? 'Unknown server error';
              break;
            }
          } catch { /* skip malformed chunks */ }
        }
        if (sseError) throw new Error(sseError);
      }

      localStorage.setItem(LS_ANALYSIS_STD, accumulated);

      if (isCached && cachedMessageId !== null) {
        setMessageIdStd(cachedMessageId);
        localStorage.setItem(LS_MESSAGE_ID_STD, String(cachedMessageId));
        if (cachedSessionPath) {
          setSessionPath(cachedSessionPath);
          localStorage.setItem(LS_SESSION_PATH, cachedSessionPath);
        }
        toast.info('Result retrieved from cache');
      } else {
        const endTime = Date.now();
        const ttftS = firstTokenTime !== null ? (firstTokenTime - startTime) / 1000 : 0;
        const generationS = firstTokenTime !== null ? (endTime - firstTokenTime) / 1000 : (endTime - startTime) / 1000;
        if (volumeConfigured) {
          currentSession = await autoSavePdfs(oldPdf, newPdf);
          if (currentSession) setSessionPath(currentSession);
        }
        if (currentSession) {
          await autoSaveResult(currentSession, 'analysis.md', accumulated);
          const docTitle = `${oldPdf.name} vs ${newPdf.name}`;
          autoSavePdf(currentSession, accumulated, docTitle);
        }
        const mid = await saveToHistory(
          oldPdf.name, newPdf.name, accumulated, '', currentSession, ohash, nhash,
          fileType, 'standard', 'standard', ttftS, generationS, usageData, llmRequestId,
        );
        if (mid !== null) {
          setMessageIdStd(mid);
          localStorage.setItem(LS_MESSAGE_ID_STD, String(mid));
        }
      }
    } catch (err) {
      if (err instanceof Error && err.name === 'AbortError') {
        toast.info('Analysis cancelled — partial result kept.');
        return;
      }
      const raw = err instanceof Error ? err.message : String(err);
      const isNetworkDrop = raw.toLowerCase().includes('failed to fetch')
        || raw.toLowerCase().includes('networkerror')
        || raw.toLowerCase().includes('load failed');
      const msg = isNetworkDrop
        ? 'Connection lost during analysis. Please retry — the server may still be processing.'
        : raw;
      setError(msg);
      toast.error(`Change Summary failed: ${msg}`);
    } finally {
      setIsAnalyzingStd(false);
    }
  };

  const handleAnalyzeBoth = (forceRefresh = false) => {
    void Promise.allSettled([handleAnalyzeStructured(forceRefresh), handleAnalyzeStandard(forceRefresh)]);
  };

  // -------------------------------------------------------------------------
  // Impact — uses whichever main analysis ran (Change Table preferred,
  // falls back to Change Summary since either produces a usable changes summary),
  // or a hand-typed change description in 'manual' mode.
  // -------------------------------------------------------------------------

  const changesTextForImpact = effectiveImpactMode === 'manual' ? manualChangesText.trim() : (analysis || analysisStd);

  const handleImpact = async (forceRefresh = false) => {
    if (!changesTextForImpact) return;
    setIsImpacting(true);
    setImpact(null);
    setImpactError('');
    localStorage.removeItem(LS_IMPACT);
    try {
      const manual = effectiveImpactMode === 'manual';
      const res = await fetch('/api/compare/impact', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          changes_text: changesTextForImpact,
          // Link the impact_requests audit row back to this comparison's messages row
          // — also the cache key the server looks up unless force_refresh is set.
          // No comparison ran in manual mode, so no hash to key the cache on.
          old_file_hash: manual ? '' : (oldFileHash || ''),
          new_file_hash: manual ? '' : (newFileHash || ''),
          // The compared document itself would be the top hit — the server drops it.
          old_file_name: manual ? '' : (oldPdf?.name || ''),
          new_file_name: manual ? '' : (newPdf?.name || ''),
          force_refresh: forceRefresh,
        }),
      });
      if (!res.ok || !res.body) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || `Server error: ${res.status}`);
      }
      // NDJSON stream: plan → one document per judged candidate → done | error.
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let current: ImpactResult | null = null;
      const handleEvent = (ev: Record<string, unknown>) => {
        if (ev.type === 'error') throw new Error(String(ev.error));
        if (ev.type === 'plan') {
          current = { ...(ev as unknown as ImpactResult), documents: [], done: false };
        } else if (ev.type === 'document' && current) {
          current = { ...current, documents: [...current.documents, ev.document as ImpactResult['documents'][number]] };
        } else if (ev.type === 'done' && current) {
          current = { ...current, done: true, usage: ev.usage as ImpactResult['usage'], duration_s: ev.duration_s as number };
        }
        setImpact(current);
      };
      for (;;) {
        const { done, value } = await reader.read();
        if (value) buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() ?? '';
        for (const line of lines) if (line.trim()) handleEvent(JSON.parse(line));
        if (done) break;
      }
      if (buffer.trim()) handleEvent(JSON.parse(buffer));
      const final = current as ImpactResult | null;
      if (!final?.done) throw new Error('The search was interrupted before it finished.');
      if (final.cached) toast.info('Result retrieved from cache');
      else if (final.no_changes) toast.info('Analysis reported no substantive changes — nothing to judge.');
      localStorage.setItem(LS_IMPACT, JSON.stringify(final));
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setImpactError(msg);
      toast.error(`Impact search failed: ${msg}`);
    } finally {
      setIsImpacting(false);
    }
  };

  // -------------------------------------------------------------------------
  // Single-document summary — independent of the diff; cached server-side on
  // the file's own hash, so re-summarizing the same file (even across a
  // different comparison) is instant unless "Re-run" is used.
  // -------------------------------------------------------------------------

  const handleSummarize = async (which: 'old' | 'new', forceRefresh = false) => {
    const doc = which === 'old' ? oldPdf : newPdf;
    if (!doc) return;
    setActiveSummaryTab(which);
    setSummaries(prev => ({ ...prev, [which]: { text: '', error: '', loading: true, noContent: false, meta: null } }));
    try {
      const hash = await computeFileHash(doc.bytes);
      const form = new FormData();
      form.append('file', new Blob([doc.bytes], { type: doc.mimeType }), doc.name);
      form.append('file_hash', hash);
      form.append('force_refresh', forceRefresh ? 'true' : 'false');
      const res = await fetch('/api/compare/summarize', { method: 'POST', body: form });
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || `Server error: ${res.status}`);
      setSummaries(prev => ({
        ...prev,
        [which]: {
          text: data.summary || '',
          error: '',
          loading: false,
          noContent: !!data.no_content,
          meta: {
            truncated: !!data.truncated,
            durationS: data.duration_s ?? 0,
            inputTokens: data.usage?.input_tokens ?? 0,
            outputTokens: data.usage?.output_tokens ?? 0,
            totalTokens: data.usage?.total_tokens ?? 0,
            costEur: data.usage?.cost_eur ?? 0,
          },
        },
      }));
      if (data.cached) toast.info('Result retrieved from cache');
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setSummaries(prev => ({ ...prev, [which]: { ...prev[which], loading: false, error: msg } }));
      toast.error(`Summary failed: ${msg}`);
    }
  };

  const downloadSummaryPdf = async (which: 'old' | 'new') => {
    const doc = which === 'old' ? oldPdf : newPdf;
    const text = summaries[which].text;
    if (!doc || !text) return;
    const base = doc.name.replace(/\.[^.]+$/, '') || 'document';
    const form = new FormData();
    form.append('markdown_text', text);
    form.append('title', `Summary — ${doc.name}`);
    form.append('filename', `summary_${base}`);
    try {
      const res = await fetch('/api/compare/export-pdf', { method: 'POST', body: form });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
        toast.error(`PDF export failed: ${err.error ?? res.statusText}`);
        return;
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = `summary_${base}.pdf`; a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      toast.error(`PDF export failed: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const canAnalyze = !!oldPdf && !!newPdf && !!oldPdf.data && !!newPdf.data && !isAnalyzing && !isAnalyzingStd && !isImpacting;
  const hasStandardVersion = availableVersions.some(v => v.id === 'standard');
  const hasStructuredVersion = availableVersions.some(v => v.id === 'structured');

  const accentBlue   = 'var(--color-accent-primary)';
  const accentAmber  = '#d97706';
  const accentGreen  = '#16a34a';

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------

  return (
    <div className="max-w-7xl mx-auto px-4 py-10 space-y-8">

      {/* ── Header ── */}
      <div className="flex items-start justify-between gap-4">
        <div className="space-y-1">
          <h1 className="text-2xl font-bold tracking-tight" style={{ color: 'var(--color-text-heading)', fontFamily: 'var(--font-heading)' }}>
            Document Comparison
          </h1>
          <p className="text-sm" style={{ color: 'var(--color-text-muted)' }}>
            Upload two documents (PDF, DOCX, PPTX, Excel, image…) to detect differences.
          </p>
        </div>
        <div className="flex-shrink-0 flex items-center gap-2">
          <WhatsNewButton tabId="compare" open={showWhatsNew} onClick={() => setShowWhatsNew(o => !o)} />
          {(oldPdf || newPdf || analysis || analysisStd || manualChangesText) && (
            <button
              onClick={clearAll}
              disabled={isAnalyzing || isAnalyzingStd || isImpacting}
              className="flex items-center gap-1.5 px-3 py-2 rounded-xl text-sm font-medium border transition-all cursor-pointer disabled:opacity-40"
              style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-muted)', background: 'transparent' }}
              onMouseEnter={e => { e.currentTarget.style.borderColor = 'var(--color-error)'; e.currentTarget.style.color = 'var(--color-error)'; }}
              onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--color-border)'; e.currentTarget.style.color = 'var(--color-text-muted)'; }}
            >
              <Trash2 className="h-4 w-4" />
              Clear all
            </button>
          )}
          <button
            onClick={() => setHistoryOpen(true)}
            className="flex items-center gap-2 px-3.5 py-2 rounded-xl text-sm font-medium border transition-all cursor-pointer"
            style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-muted)', background: 'transparent' }}
            onMouseEnter={e => { e.currentTarget.style.borderColor = 'var(--color-accent-primary)'; e.currentTarget.style.color = 'var(--color-accent-primary)'; }}
            onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--color-border)'; e.currentTarget.style.color = 'var(--color-text-muted)'; }}
          >
            <History className="h-4 w-4" />
            History
          </button>
        </div>
      </div>

      {showWhatsNew && <WhatsNewModal tabId="compare" onClose={() => setShowWhatsNew(false)} />}

      {/* ── Disclaimer banner ── */}
      <div className="flex items-start gap-3 px-4 py-3.5 rounded-xl border" style={{ borderColor: '#d97706', background: '#fffbeb' }}>
        <Info className="h-4 w-4 mt-0.5 flex-shrink-0" style={{ color: '#d97706' }} />
        <p className="text-sm leading-relaxed" style={{ color: '#92400e' }}>
          This tool is intended to compare two versions of the same document.
        </p>
      </div>

      <ComparisonHistory
        open={historyOpen}
        onClose={() => setHistoryOpen(false)}
        onLoad={handleLoadFromHistory}
      />

      {/* ── Error banner ── */}
      {error && (
        <div className="flex items-start gap-3 px-4 py-3.5 rounded-xl border border-[var(--color-error)]/25 bg-[var(--color-error)]/5">
          <AlertCircle className="h-4 w-4 text-[var(--color-error)] mt-0.5 flex-shrink-0" />
          <p className="text-sm text-[var(--color-error)] leading-relaxed">{error}</p>
        </div>
      )}

      {/* ── Upload + Actions card ── */}
      <div className="rounded-2xl border border-[var(--color-border)]/40 bg-[var(--color-bg-secondary)]/30 p-6 space-y-5">

        {/* Drop zones */}
        <div className="flex gap-4">
          <PdfDropZone
            label="Document A (Old)"
            step={1}
            file={oldPdf}
            onFile={(f) => { clearAnalysisState(); setSummaries(prev => ({ ...prev, old: EMPTY_SUMMARY })); setOldPdfAndPersist(f); }}
            onRemove={() => { clearAnalysisState(); setSummaries(prev => ({ ...prev, old: EMPTY_SUMMARY })); setOldPdfAndPersist(null); }}
            onInvalidFile={handleInvalidFile}
            disabled={isAnalyzing || isAnalyzingStd || isImpacting}
          />
          <PdfDropZone
            label="Document B (New)"
            step={2}
            file={newPdf}
            onFile={(f) => { clearAnalysisState(); setSummaries(prev => ({ ...prev, new: EMPTY_SUMMARY })); setNewPdfAndPersist(f); }}
            onRemove={() => { clearAnalysisState(); setSummaries(prev => ({ ...prev, new: EMPTY_SUMMARY })); setNewPdfAndPersist(null); }}
            onInvalidFile={handleInvalidFile}
            disabled={isAnalyzing || isAnalyzingStd || isImpacting}
          />
        </div>

        {/* Quick per-document summary — independent of the diff, works as soon as either file is uploaded */}
        {(oldPdf || newPdf) && (
          <div className="flex flex-wrap items-center gap-3">
            {oldPdf && (
              <>
                <button
                  onClick={() => handleSummarize('old')}
                  disabled={summaries.old.loading}
                  className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-semibold transition-all duration-150 border cursor-pointer disabled:opacity-40"
                  style={{ borderColor: accentGreen, color: accentGreen, background: 'transparent' }}
                  title="Summarize Document A on its own — no comparison needed"
                >
                  {summaries.old.loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileText className="h-4 w-4" />}
                  {summaries.old.loading ? 'Summarizing…' : 'Summarize Document A'}
                </button>
                {summaries.old.text && !summaries.old.loading && (
                  <button
                    onClick={() => handleSummarize('old', true)}
                    className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-semibold transition-all duration-150 border cursor-pointer"
                    style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-muted)', background: 'transparent' }}
                    onMouseEnter={e => { e.currentTarget.style.borderColor = 'var(--color-accent-primary)'; e.currentTarget.style.color = 'var(--color-accent-primary)'; }}
                    onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--color-border)'; e.currentTarget.style.color = 'var(--color-text-muted)'; }}
                    title="Force a fresh summary of Document A, ignoring cached results"
                  >
                    <RotateCcw className="h-4 w-4" />
                    Re-run
                  </button>
                )}
              </>
            )}
            {newPdf && (
              <>
                <button
                  onClick={() => handleSummarize('new')}
                  disabled={summaries.new.loading}
                  className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-semibold transition-all duration-150 border cursor-pointer disabled:opacity-40"
                  style={{ borderColor: accentGreen, color: accentGreen, background: 'transparent' }}
                  title="Summarize Document B on its own — no comparison needed"
                >
                  {summaries.new.loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileText className="h-4 w-4" />}
                  {summaries.new.loading ? 'Summarizing…' : 'Summarize Document B'}
                </button>
                {summaries.new.text && !summaries.new.loading && (
                  <button
                    onClick={() => handleSummarize('new', true)}
                    className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-semibold transition-all duration-150 border cursor-pointer"
                    style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-muted)', background: 'transparent' }}
                    onMouseEnter={e => { e.currentTarget.style.borderColor = 'var(--color-accent-primary)'; e.currentTarget.style.color = 'var(--color-accent-primary)'; }}
                    onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--color-border)'; e.currentTarget.style.color = 'var(--color-text-muted)'; }}
                    title="Force a fresh summary of Document B, ignoring cached results"
                  >
                    <RotateCcw className="h-4 w-4" />
                    Re-run
                  </button>
                )}
              </>
            )}
          </div>
        )}

        {(() => {
          const oldActive = !!(summaries.old.text || summaries.old.loading || summaries.old.error || summaries.old.noContent);
          const newActive = !!(summaries.new.text || summaries.new.loading || summaries.new.error || summaries.new.noContent);
          if (!oldActive && !newActive) return null;
          const bothActive = oldActive && newActive;
          // Always show exactly one card, full width — a tab switch when both are
          // active, rather than splitting the width between two half-size cards.
          const shown: 'old' | 'new' = bothActive ? activeSummaryTab : (oldActive ? 'old' : 'new');
          return (
            <div className="space-y-3">
              {bothActive && (
                <div className="inline-flex rounded-xl border border-[var(--color-border)]/40 p-1 gap-1">
                  {(['old', 'new'] as const).map(which => (
                    <button
                      key={which}
                      onClick={() => setActiveSummaryTab(which)}
                      className="px-3.5 py-1.5 rounded-lg text-sm font-medium transition-all cursor-pointer"
                      style={shown === which
                        ? { background: accentGreen, color: 'white' }
                        : { color: 'var(--color-text-muted)', background: 'transparent' }}
                    >
                      {which === 'old' ? 'Document A' : 'Document B'}
                    </button>
                  ))}
                </div>
              )}
              <DocSummaryCard
                title={shown === 'old' ? 'Document A Summary' : 'Document B Summary'}
                state={shown === 'old' ? summaries.old : summaries.new}
                accentColor={accentGreen}
                onDownload={() => downloadSummaryPdf(shown)}
              />
            </div>
          );
        })()}

        {/* Divider */}
        <div className="border-t border-[var(--color-border)]/30" />

        {/* Actions row — no version selector: both outputs are independent buttons */}
        <div className="flex flex-wrap items-center gap-3">
          {hasStandardVersion && (
            <button
              onClick={() => handleAnalyzeStandard()}
              disabled={!canAnalyze}
              className="flex items-center gap-2 px-5 py-2.5 rounded-xl text-sm font-semibold text-white shadow-sm transition-all duration-150 disabled:opacity-40 disabled:cursor-not-allowed disabled:shadow-none cursor-pointer"
              style={{
                background: canAnalyze
                  ? 'linear-gradient(135deg, var(--color-accent-primary) 0%, var(--color-accent-secondary) 100%)'
                  : 'var(--color-border)',
                boxShadow: canAnalyze ? '0 2px 8px rgba(0,85,164,0.35)' : undefined,
              }}
              title="Narrative diff grouped by section — one bullet per change."
            >
              {isAnalyzingStd
                ? <Loader2 className="h-4 w-4 animate-spin" />
                : <ArrowRight className="h-4 w-4" />}
              {isAnalyzingStd ? 'Generating…' : 'Generate Change Summary'}
            </button>
          )}
          {isAnalyzingStd && (
            <button
              onClick={() => abortControllerRefStd.current?.abort()}
              className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-semibold transition-all duration-150 border cursor-pointer"
              style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-body)' }}
            >
              <Square className="h-3.5 w-3.5" />
              Cancel
            </button>
          )}
          {analysisStd && canAnalyze && (
            <button
              onClick={() => handleAnalyzeStandard(true)}
              className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-semibold transition-all duration-150 border cursor-pointer"
              style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-muted)', background: 'transparent' }}
              onMouseEnter={e => { e.currentTarget.style.borderColor = 'var(--color-accent-primary)'; e.currentTarget.style.color = 'var(--color-accent-primary)'; }}
              onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--color-border)'; e.currentTarget.style.color = 'var(--color-text-muted)'; }}
              title="Force a fresh Change Summary, ignoring cached results"
            >
              <RotateCcw className="h-4 w-4" />
              Re-run
            </button>
          )}

          {hasStructuredVersion && (
            <button
              onClick={() => handleAnalyzeStructured()}
              disabled={!canAnalyze}
              className="flex items-center gap-2 px-5 py-2.5 rounded-xl text-sm font-semibold transition-all duration-150 disabled:opacity-40 disabled:cursor-not-allowed border cursor-pointer"
              style={{ borderColor: accentBlue, color: canAnalyze ? accentBlue : 'var(--color-text-muted)', background: 'transparent' }}
              title="Structured change table with type and criticality. Exportable to Excel."
            >
              {isAnalyzing
                ? <Loader2 className="h-4 w-4 animate-spin" />
                : <ArrowRight className="h-4 w-4" />}
              {isAnalyzing ? 'Generating…' : 'Generate Change Table'}
            </button>
          )}
          {isAnalyzing && (
            <button
              onClick={() => abortControllerRef.current?.abort()}
              className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-semibold transition-all duration-150 border cursor-pointer"
              style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-body)' }}
            >
              <Square className="h-3.5 w-3.5" />
              Cancel
            </button>
          )}
          {analysis && canAnalyze && (
            <button
              onClick={() => handleAnalyzeStructured(true)}
              className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-semibold transition-all duration-150 border cursor-pointer"
              style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-muted)', background: 'transparent' }}
              onMouseEnter={e => { e.currentTarget.style.borderColor = 'var(--color-accent-primary)'; e.currentTarget.style.color = 'var(--color-accent-primary)'; }}
              onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--color-border)'; e.currentTarget.style.color = 'var(--color-text-muted)'; }}
              title="Force a fresh Change Table, ignoring cached results"
            >
              <RotateCcw className="h-4 w-4" />
              Re-run
            </button>
          )}

          {hasStandardVersion && hasStructuredVersion && (
            <button
              onClick={() => handleAnalyzeBoth()}
              disabled={!canAnalyze}
              className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-semibold transition-all duration-150 border cursor-pointer disabled:opacity-40"
              style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-muted)', background: 'transparent' }}
              onMouseEnter={e => { e.currentTarget.style.borderColor = 'var(--color-accent-primary)'; e.currentTarget.style.color = 'var(--color-accent-primary)'; }}
              onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--color-border)'; e.currentTarget.style.color = 'var(--color-text-muted)'; }}
            >
              <ArrowRight className="h-4 w-4" />
              Run Both
            </button>
          )}
        </div>

      </div>

      {/* ── Results ── */}
      {(analysis || isAnalyzing || analysisStd || isAnalyzingStd) && currentFileType === 'docx' && (
        <p className="text-xs text-[var(--color-text-muted)] flex items-center gap-1.5">
          <span>⚠️</span>
          <span><strong>Page numbers are approximate</strong> (DOCX limitation) — actual change may be on an adjacent page.</span>
        </p>
      )}
      {(() => {
        const stdActive = !!(analysisStd || isAnalyzingStd);
        const structActive = !!(analysis || isAnalyzing);
        if (!stdActive && !structActive) return null;
        const bothActive = stdActive && structActive;
        // Always show exactly one card, full width — a tab switch when both are
        // active, rather than splitting the width between two half-size cards
        // (same reasoning as the Document Summary A/B tab switcher above).
        const shown: 'summary' | 'table' = bothActive ? activeAnalysisTab : (stdActive ? 'summary' : 'table');
        return (
          <div className="space-y-3">
            {bothActive && (
              <div className="inline-flex rounded-xl border border-[var(--color-border)]/40 p-1 gap-1">
                {(['summary', 'table'] as const).map(which => (
                  <button
                    key={which}
                    onClick={() => setActiveAnalysisTab(which)}
                    className="px-3.5 py-1.5 rounded-lg text-sm font-medium transition-all cursor-pointer"
                    style={shown === which
                      ? { background: accentBlue, color: 'white' }
                      : { color: 'var(--color-text-muted)', background: 'transparent' }}
                  >
                    {which === 'summary' ? 'Change Summary' : 'Change Table'}
                  </button>
                ))}
              </div>
            )}
            {shown === 'summary' ? (
              <ResultCard
                title="Change Summary"
                icon={FileText}
                accentColor={accentBlue}
                content={analysisStd}
                isStreaming={isAnalyzingStd}
                onDownload={async () => {
                  const base = newPdf?.name.replace(/\.[^.]+$/, '') ?? 'document';
                  const form = new FormData();
                  form.append('markdown_text', analysisStd);
                  form.append('title', `Analysis — ${base}`);
                  form.append('filename', `analysis_${base}`);
                  try {
                    const res = await fetch('/api/compare/export-pdf', { method: 'POST', body: form });
                    if (!res.ok) {
                      const err = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
                      toast.error(`PDF export failed: ${err.error ?? res.statusText}`);
                      return;
                    }
                    const blob = await res.blob();
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url; a.download = `analysis_${base}.pdf`; a.click();
                    URL.revokeObjectURL(url);
                  } catch (e) {
                    toast.error(`PDF export failed: ${e instanceof Error ? e.message : String(e)}`);
                  }
                }}
                feedbackProps={
                  analysisStd && !isAnalyzingStd && oldPdf && newPdf
                    ? {
                        submissionKey: messageIdStd !== null
                          ? `mid-${messageIdStd}`
                          : `hash-${oldFileHash.slice(0, 16)}-${newFileHash.slice(0, 16)}-std`,
                        messageId: messageIdStd,
                      }
                    : undefined
                }
              />
            ) : (
              <StructuredResultCard
                items={parsedJsonItems}
                isStreaming={isAnalyzing}
                accentColor={accentBlue}
                fileType={currentFileType}
                imageContext={imageContext}
                onExportExcel={async () => {
                  try {
                    const base = newPdf?.name.replace(/\.[^.]+$/, '') ?? 'comparison';
                    const form = new FormData();
                    form.append('json_text', analysis);
                    form.append('filename', `analysis_${base}`);
                    form.append('file_type', currentFileType);
                    if (imageContext.length > 0) {
                      form.append('image_pairs_json', JSON.stringify(imageContext));
                    }
                    const res = await fetch('/api/compare/export-excel', { method: 'POST', body: form });
                    if (!res.ok) {
                      const err = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
                      toast.error(`Excel export failed: ${err.error ?? res.statusText}`);
                      return;
                    }
                    const blob = await res.blob();
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = `analysis_${base}.xlsx`;
                    a.click();
                    URL.revokeObjectURL(url);
                  } catch (e) {
                    toast.error(`Excel export failed: ${e instanceof Error ? e.message : String(e)}`);
                  }
                }}
                feedbackProps={
                  analysis && !isAnalyzing && oldPdf && newPdf
                    ? {
                        submissionKey: messageId !== null
                          ? `mid-${messageId}`
                          : `hash-${oldFileHash.slice(0, 16)}-${newFileHash.slice(0, 16)}`,
                        messageId,
                      }
                    : undefined
                }
              />
            )}
          </div>
        );
      })()}

      {impactConfigured && (
        <div className="space-y-3">
          <div className="rounded-xl border px-4 py-2.5 text-xs" style={{ borderColor: '#d97706', background: '#fffbeb', color: '#92400e' }}>
            This section is in testing phase — results may be incomplete or inaccurate.
          </div>

          <div className="flex flex-wrap items-center justify-between gap-3">
            <h2 className="text-sm font-semibold" style={{ color: 'var(--color-text-heading)' }}>Impact Search</h2>
            {IMPACT_MANUAL_MODE_ENABLED && (
              <div className="inline-flex rounded-xl border border-[var(--color-border)]/40 p-1 gap-1">
                <button
                  onClick={() => switchImpactMode('compare')}
                  className="px-3.5 py-1.5 rounded-lg text-sm font-medium transition-all cursor-pointer"
                  style={effectiveImpactMode === 'compare'
                    ? { background: accentAmber, color: 'white' }
                    : { color: 'var(--color-text-muted)', background: 'transparent' }}
                >
                  Use Compare result
                </button>
                <button
                  onClick={() => switchImpactMode('manual')}
                  className="px-3.5 py-1.5 rounded-lg text-sm font-medium transition-all cursor-pointer"
                  style={effectiveImpactMode === 'manual'
                    ? { background: accentAmber, color: 'white' }
                    : { color: 'var(--color-text-muted)', background: 'transparent' }}
                  title="No old/new document to upload — just describe the change (e.g. from an email) to search for impacted documents"
                >
                  Describe a change manually
                </button>
              </div>
            )}
          </div>

          {effectiveImpactMode === 'manual' ? (
            <textarea
              value={manualChangesText}
              onChange={(e) => setManualChangesTextAndPersist(e.target.value)}
              disabled={isImpacting}
              placeholder="Paste or type a description of the change — e.g. an email announcing a new torque value, a general idea of what's changing…"
              rows={5}
              className="w-full rounded-xl border p-4 text-sm resize-y disabled:opacity-60"
              style={{ borderColor: 'var(--color-border)', background: 'var(--color-bg-primary)', color: 'var(--color-text-body)' }}
            />
          ) : (
            changesTextForImpact && !isAnalyzing && !isAnalyzingStd && (
              <p className="text-xs -mb-1" style={{ color: 'var(--color-text-muted)' }}>
                Using: <strong>{analysis ? 'Change Table' : 'Change Summary'}</strong> as the change summary
                {analysis && analysisStd && ' (Change Table takes priority when both are available)'}
              </p>
            )
          )}
        </div>
      )}
      {changesTextForImpact && !isAnalyzing && !isAnalyzingStd && impactConfigured && (
        <div className="flex flex-wrap items-center gap-3">
          <button
            onClick={() => handleImpact()}
            disabled={isImpacting}
            className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-semibold transition-all duration-150 border cursor-pointer disabled:opacity-40"
            style={{ borderColor: accentAmber, color: accentAmber, background: 'transparent' }}
          >
            {isImpacting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Database className="h-4 w-4" />}
            {isImpacting ? 'Judging…' : 'Judge Impacted Docs'}
          </button>
          {impact?.done && !isImpacting && (
            <button
              onClick={() => handleImpact(true)}
              className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-semibold transition-all duration-150 border cursor-pointer"
              style={{ borderColor: 'var(--color-border)', color: 'var(--color-text-muted)', background: 'transparent' }}
              onMouseEnter={e => { e.currentTarget.style.borderColor = 'var(--color-accent-primary)'; e.currentTarget.style.color = 'var(--color-accent-primary)'; }}
              onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--color-border)'; e.currentTarget.style.color = 'var(--color-text-muted)'; }}
              title="Force a fresh search, ignoring cached results"
            >
              <RotateCcw className="h-4 w-4" />
              Re-run
            </button>
          )}
        </div>
      )}

      {(impact || isImpacting || impactError) && (
        <ImpactResultsCard
          result={impact}
          isLoading={isImpacting}
          error={impactError}
          accentColor={accentAmber}
          exportName={`impact_${(newPdf?.name || oldPdf?.name || 'search').replace(/\.[^.]+$/, '')}`}
        />
      )}
    </div>
  );
}
