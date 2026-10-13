import { useRef, useState, type DragEvent } from 'react';
import { Download, Eye, FileText, Upload, X } from 'lucide-react';
import { FILE_ACCEPT, MAX_FILE_SIZE_BYTES, SUPPORTED_EXTENSIONS, extensionOf, formatFileSize, readFile } from './files';
import { FilePreviewModal, useOfficePreview } from './FilePreviewModal';
import type { DocFile } from './types';

const AMBER = '#d97706';
const ACCENT_RGB = 'var(--color-accent-primary-rgb,0,85,164)';

const ICON_BUTTON = 'flex-shrink-0 w-7 h-7 rounded-lg flex items-center justify-center text-[var(--color-text-muted)] transition-all cursor-pointer';
const ICON_BUTTON_ACCENT = `${ICON_BUTTON} hover:text-[var(--color-accent-primary)] hover:bg-[var(--color-accent-primary)]/10`;

export function DocDropZone({ label, step, file, onFile, onRemove, onInvalidFile, disabled }: {
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
  const preview = useOfficePreview(file);
  const inputRef = useRef<HTMLInputElement>(null);

  const handleFile = async (f: File) => {
    const ext = extensionOf(f.name);
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
  };

  const dragHandlers = {
    onDrop: (e: DragEvent) => {
      e.preventDefault();
      setIsDragOver(false);
      const f = e.dataTransfer.files?.[0];
      if (f) handleFile(f);
    },
    onDragOver: (e: DragEvent) => { e.preventDefault(); setIsDragOver(true); },
    onDragLeave: () => setIsDragOver(false),
  };

  if (file) {
    // A placeholder is a history entry whose bytes could not be restored.
    const isPlaceholder = !file.data;
    const tone = isPlaceholder ? AMBER : 'var(--color-accent-primary)';
    const borderColor = isPlaceholder
      ? (isDragOver ? AMBER : 'rgba(217,119,6,0.4)')
      : (isDragOver ? 'var(--color-accent-primary)' : `rgba(${ACCENT_RGB},0.3)`);
    const bgColor = isPlaceholder
      ? (isDragOver ? 'rgba(217,119,6,0.12)' : 'rgba(217,119,6,0.05)')
      : (isDragOver ? `rgba(${ACCENT_RGB},0.1)` : `rgba(${ACCENT_RGB},0.05)`);
    return (
      <>
        <div
          className="flex-1 min-w-0 rounded-2xl p-4 transition-all"
          style={{ border: `1px solid ${borderColor}`, background: bgColor, cursor: disabled ? 'default' : 'copy' }}
          {...(disabled ? {} : dragHandlers)}
        >
          {isPlaceholder && <p className="text-xs font-semibold mb-2" style={{ color: AMBER }}>Drop file to re-upload</p>}
          <div className="flex items-center gap-3">
            <div className="flex-shrink-0 w-8 h-8 rounded-full flex items-center justify-center" style={{ background: tone }}>
              <span className="text-xs font-bold text-white">{step}</span>
            </div>
            <div
              className="flex-shrink-0 w-9 h-9 rounded-xl flex items-center justify-center"
              style={{ background: isPlaceholder ? 'rgba(217,119,6,0.1)' : `rgba(${ACCENT_RGB},0.1)` }}
            >
              <FileText className="h-4 w-4" style={{ color: tone }} />
            </div>
            <div className="min-w-0 flex-1">
              <p className="text-xs font-semibold uppercase tracking-wide mb-0.5" style={{ color: tone }}>{label}</p>
              <p className="text-sm font-medium text-[var(--color-text-heading)] truncate">{file.name}</p>
              <p className="text-xs text-[var(--color-text-muted)]">
                {isPlaceholder ? 'File not available — drop to replace' : formatFileSize(file.size)}
              </p>
            </div>
            {!isPlaceholder && (
              <>
                <button
                  onClick={() => { setPreviewOpen(true); preview.load(); }}
                  className={ICON_BUTTON_ACCENT}
                  title="Preview file"
                >
                  <Eye className="h-3.5 w-3.5" />
                </button>
                <a
                  href={`data:${file.mimeType};base64,${file.data}`}
                  download={file.name}
                  className={ICON_BUTTON_ACCENT}
                  title="Download file"
                >
                  <Download className="h-3.5 w-3.5" />
                </a>
              </>
            )}
            {!disabled && (
              <button
                onClick={onRemove}
                className={`${ICON_BUTTON} hover:text-[var(--color-error)] hover:bg-[var(--color-error)]/10`}
                title="Remove file"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            )}
          </div>
        </div>

        {previewOpen && <FilePreviewModal label={label} file={file} preview={preview} onClose={() => setPreviewOpen(false)} />}
      </>
    );
  }

  return (
    <div
      className={`flex-1 min-w-0 rounded-2xl border-2 border-dashed p-6 cursor-pointer group transition-all duration-200 ${
        isDragOver
          ? 'border-[var(--color-accent-primary)] bg-[var(--color-accent-primary)]/8 scale-[1.01]'
          : 'border-[var(--color-border)]/50 hover:border-[var(--color-accent-primary)]/60 hover:bg-[var(--color-accent-primary)]/3'
      } ${disabled ? 'opacity-40 pointer-events-none' : ''}`}
      {...dragHandlers}
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
