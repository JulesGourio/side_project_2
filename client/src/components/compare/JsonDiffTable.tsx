import { useEffect, useRef, useState } from 'react';
import { Loader2, X } from 'lucide-react';
import { FOCUS_CHANGE_EVENT } from './ImpactResults';
import { DiffItem, ImagePair } from './compareShared';

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

export const CRIT_STYLE: Record<string, { color: string; badge: string }> = {
  critical: { color: '#dc2626', badge: '#fef2f2' },
  high:     { color: '#ea580c', badge: '#fff7ed' },
  medium:   { color: '#d97706', badge: '#fffbeb' },
  low:      { color: '#16a34a', badge: '#f0fdf4' },
  minor:    { color: '#16a34a', badge: '#f0fdf4' },
};

export const IMAGE_KEYWORDS = ['image', 'figure', 'visual', 'photo', 'diagram', 'illustration', 'screenshot', 'picture'];

export function JsonDiffTable({
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
  // Row number clicked in the impact search results ("C12"): revealed, scrolled to, briefly highlighted.
  const [focusedChange, setFocusedChange] = useState<string | null>(null);

  useEffect(() => {
    const onFocus = (e: Event) => {
      const id = (e as CustomEvent<string>).detail;
      setHideLow(false);
      setFocusedChange(id);
    };
    window.addEventListener(FOCUS_CHANGE_EVENT, onFocus);
    return () => window.removeEventListener(FOCUS_CHANGE_EVENT, onFocus);
  }, []);

  useEffect(() => {
    if (!focusedChange) return;
    scrollContainerRef.current
      ?.querySelector(`[data-change-id="${focusedChange}"]`)
      ?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    const timer = setTimeout(() => setFocusedChange(null), 2500);
    return () => clearTimeout(timer);
  }, [focusedChange]);

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
                // Row number = the change id used by the impact search (C1, C2…).
                '#',
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
                  <tr
                    key={i}
                    data-change-id={`C${i + 1}`}
                    style={{
                      background: focusedChange === `C${i + 1}` ? '#fde68a66' : i % 2 === 0 ? 'transparent' : 'var(--color-muted)',
                      transition: 'background 0.4s',
                    }}
                  >
                    <td className="px-2 py-1.5 align-top font-mono whitespace-nowrap" style={{ borderBottom: '1px solid var(--color-border)', color: 'var(--color-text-muted)' }}>
                      C{i + 1}
                    </td>
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
