import { useEffect } from 'react';
import type { ImagePair } from './types';
import { ModalShell } from './ui';

function Side({ label, page, b64 }: { label: string; page: number | null; b64: string }) {
  return (
    <div className="flex flex-col items-center gap-1.5">
      <span className="text-xs font-semibold uppercase tracking-wide" style={{ color: 'var(--color-text-muted)' }}>
        {label}{page ? ` — page ${page}` : ''}
      </span>
      <img
        src={`data:image/jpeg;base64,${b64}`}
        alt={label.toLowerCase()}
        className="object-contain rounded-lg border border-[var(--color-border)]"
        style={{ maxWidth: '44vw', maxHeight: '78vh' }}
      />
    </div>
  );
}

/** Before/after view of a visual change, closed with Escape or a click outside. */
export function ImageLightbox({ pair, onClose }: { pair: ImagePair; onClose: () => void }) {
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [onClose]);

  return (
    <ModalShell
      title="Image comparison"
      onClose={onClose}
      backdropClassName="p-6"
      panelStyle={{ maxWidth: '95vw', maxHeight: '92vh' }}
      headerClassName="px-4 py-2.5"
    >
      <div className="flex-1 overflow-auto flex flex-wrap items-start justify-center gap-4 p-4">
        {pair.old_b64 && <Side label="Before" page={pair.old_page} b64={pair.old_b64} />}
        {pair.new_b64 && <Side label="After" page={pair.new_page} b64={pair.new_b64} />}
      </div>
    </ModalShell>
  );
}
