import { useEffect, useState } from 'react';
import { FileText, Loader2 } from 'lucide-react';

/**
 * Exact-layout docx preview, used by the Compare tab's file preview modal:
 * POST /api/preview/pdf converts the document through the server's headless
 * LibreOffice engine and the browser's native PDF viewer renders it — true
 * page layout with zoom/search/page-nav built in. Conversions are cached
 * server-side by content hash, so re-opening the same file is instant.
 */
export function ExactDocxPreview({ bytes, filename }: { bytes: ArrayBuffer | Uint8Array; filename: string }) {
  const [pdfUrl, setPdfUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    let url: string | null = null;
    setPdfUrl(null);
    setError(null);
    const fd = new FormData();
    fd.append('file', new Blob([bytes as BlobPart]), filename);
    fetch('/api/preview/pdf', { method: 'POST', body: fd })
      .then(async res => {
        if (cancelled) return;
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(body?.error || res.statusText);
        }
        url = URL.createObjectURL(await res.blob());
        setPdfUrl(url);
      })
      .catch(e => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : 'Preview failed');
      });
    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [bytes, filename, attempt]);

  if (error) {
    return (
      <div className="w-full h-full flex flex-col items-center justify-center gap-3 p-6 text-center" style={{ color: 'var(--color-text-muted)' }}>
        <FileText className="h-12 w-12 opacity-30" />
        <p className="text-sm">Preview unavailable</p>
        <p className="text-xs opacity-70 max-w-md break-words">{error}</p>
        <button
          onClick={() => setAttempt(a => a + 1)}
          className="text-xs px-2.5 py-1 rounded-md cursor-pointer"
          style={{ color: 'var(--color-text-muted)', border: '1px solid var(--color-border)' }}
        >
          Retry
        </button>
      </div>
    );
  }

  if (!pdfUrl) {
    return (
      <div className="w-full h-full flex flex-col items-center justify-center gap-3" style={{ color: 'var(--color-text-muted)' }}>
        <Loader2 className="h-6 w-6 animate-spin" style={{ color: 'var(--color-accent-primary)' }} />
        <p className="text-xs">Rendering exact page layout…</p>
      </div>
    );
  }

  return <iframe title={filename} src={pdfUrl} className="w-full h-full" style={{ border: 'none' }} />;
}
