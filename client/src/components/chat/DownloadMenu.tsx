import { useEffect, useRef, useState } from 'react';
import { Download } from 'lucide-react';
import { toast } from 'sonner';
import { downloadResponsePdf, downloadSessionPdf, responseMarkdown, sessionMarkdown } from './chatExport';
import type { Message } from './message';

function saveMarkdown(content: string, filename: string) {
  const url = URL.createObjectURL(new Blob([content], { type: 'text/markdown' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export function DownloadMenu({ message, allMessages }: { message: Message; allMessages: Message[] }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onMouseDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onMouseDown);
    return () => document.removeEventListener('mousedown', onMouseDown);
  }, [open]);

  const slug = new Date().toISOString().slice(0, 10);
  const options: { label: string; action: () => void | Promise<void> }[] = [
    { label: 'Response — Markdown', action: () => saveMarkdown(responseMarkdown(message), `qualibot-response-${slug}.md`) },
    { label: 'Response — PDF', action: () => downloadResponsePdf(message, allMessages, slug) },
    { label: 'Full session — Markdown', action: () => saveMarkdown(sessionMarkdown(allMessages), `qualibot-session-${slug}.md`) },
    { label: 'Full session — PDF', action: () => downloadSessionPdf(allMessages, slug) },
  ];

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen(o => !o)}
        className="flex items-center gap-1 px-2 py-1 rounded-lg text-xs border transition-all cursor-pointer bg-transparent border-[var(--color-border)] text-[var(--color-text-muted)] hover:border-[var(--color-accent-primary)] hover:text-[var(--color-text-primary)]"
        title="Download"
      >
        <Download className="h-3.5 w-3.5" />
        <span>Télécharger</span>
      </button>

      {open && (
        <div
          className="absolute bottom-full right-0 mb-1 rounded-xl border shadow-xl z-50 overflow-hidden min-w-[220px]"
          style={{ background: 'var(--color-bg-secondary)', borderColor: 'var(--color-border)' }}
        >
          {options.map(opt => (
            <button
              key={opt.label}
              onClick={() => {
                setOpen(false);
                Promise.resolve(opt.action()).catch((e: unknown) =>
                  toast.error(`PDF export failed: ${e instanceof Error ? e.message : String(e)}`));
              }}
              className="w-full text-left px-4 py-2.5 text-xs transition-colors cursor-pointer text-[var(--color-text-primary)] hover:bg-[var(--color-bg-tertiary)]"
            >
              {opt.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
