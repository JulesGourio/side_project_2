import { useState } from 'react';
import { FileText, Loader2 } from 'lucide-react';
import { ExactDocxPreview } from '@/components/shared/ExactDocxPreview';
import { DOCX_MIME_TYPE, PDF_MIME_TYPE, docBlob } from './files';
import type { DocFile } from './types';
import { ModalShell } from './ui';

/** Office formats other than .docx are converted to HTML by the server; the result is kept per file. */
export function useOfficePreview(file: DocFile | null) {
  const [cached, setCached] = useState<{ key: string; html: string } | null>(null);
  const [loading, setLoading] = useState(false);
  const key = file ? `${file.name}:${file.size}` : null;
  const html = cached && cached.key === key ? cached.html : null;
  const needsServer = !!file && file.mimeType !== PDF_MIME_TYPE && !file.mimeType.startsWith('image/') && file.mimeType !== DOCX_MIME_TYPE;

  const load = async () => {
    if (!file || !key || !needsServer || html) return;
    setLoading(true);
    try {
      const form = new FormData();
      form.append('file', docBlob(file), file.name);
      const res = await fetch('/api/preview', { method: 'POST', body: form });
      if (res.ok) setCached({ key, html: await res.text() });
    } catch { /* best-effort */ }
    finally { setLoading(false); }
  };

  return { html, loading, load };
}

function Unavailable({ name }: { name: string }) {
  return (
    <div className="w-full h-full flex flex-col items-center justify-center gap-4" style={{ color: 'var(--color-text-muted)' }}>
      <FileText className="h-16 w-16 opacity-30" />
      <p className="text-sm">Preview unavailable</p>
      <p className="text-xs opacity-60">{name}</p>
    </div>
  );
}

export function FilePreviewModal({ label, file, preview, onClose }: {
  label: string;
  file: DocFile;
  preview: ReturnType<typeof useOfficePreview>;
  onClose: () => void;
}) {
  const { mimeType, data, name } = file;
  let body;
  if (mimeType === PDF_MIME_TYPE) {
    body = <iframe src={`data:application/pdf;base64,${data}`} className="w-full h-full border-0" title={name} />;
  } else if (mimeType.startsWith('image/')) {
    body = (
      <div className="w-full h-full flex items-center justify-center p-4 overflow-auto">
        <img src={`data:${mimeType};base64,${data}`} alt={name} className="max-w-full max-h-full object-contain rounded-lg" />
      </div>
    );
  } else if (mimeType === DOCX_MIME_TYPE) {
    body = <ExactDocxPreview bytes={file.bytes} filename={name} />;
  } else if (preview.loading) {
    body = (
      <div className="w-full h-full flex flex-col items-center justify-center gap-3" style={{ color: 'var(--color-text-muted)' }}>
        <Loader2 className="h-8 w-8 animate-spin" style={{ color: 'var(--color-accent-primary)' }} />
        <p className="text-sm">Generating preview…</p>
      </div>
    );
  } else if (preview.html) {
    body = <iframe srcDoc={preview.html} className="w-full h-full border-0" title={name} sandbox="allow-same-origin" />;
  } else {
    body = <Unavailable name={name} />;
  }

  return (
    <ModalShell
      title={`${label} — ${name}`}
      onClose={onClose}
      panelStyle={{ width: '90vw', maxWidth: '1300px', height: '92vh' }}
      headerClassName="px-5 py-3"
    >
      <div className="flex-1 overflow-hidden">{body}</div>
    </ModalShell>
  );
}
