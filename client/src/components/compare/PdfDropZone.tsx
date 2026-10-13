import { useCallback, useRef, useState } from 'react';
import { Download, Eye, FileText, Loader2, Upload, X } from 'lucide-react';
import { ExactDocxPreview } from '@/components/shared/ExactDocxPreview';
import { DOCX_MIME_TYPE, DocFile, FILE_ACCEPT, MAX_FILE_SIZE_BYTES, SUPPORTED_EXTENSIONS, formatFileSize, readFile } from './compareShared';

export function PdfDropZone({
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
