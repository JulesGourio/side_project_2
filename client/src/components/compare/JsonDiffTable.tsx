import { useEffect, useMemo, useState, type CSSProperties } from 'react';
import { Loader2 } from 'lucide-react';
import { FOCUS_CHANGE_EVENT } from './impact/types';
import type { DiffItem, ImagePair } from './types';
import { ImageLightbox } from './ImageLightbox';
import { useStickToBottom } from './ui';

const CRIT_STYLE: Record<string, { color: string; badge: string }> = {
  critical: { color: '#dc2626', badge: '#fef2f2' },
  high:     { color: '#ea580c', badge: '#fff7ed' },
  medium:   { color: '#d97706', badge: '#fffbeb' },
  low:      { color: '#16a34a', badge: '#f0fdf4' },
  minor:    { color: '#16a34a', badge: '#f0fdf4' },
};

const IMAGE_KEYWORDS = ['image', 'figure', 'visual', 'photo', 'diagram', 'illustration', 'screenshot', 'picture'];

/** Header of the page column by file type; `null` = no such column (spreadsheets). */
const PAGE_LABEL: Record<string, string | null> = { pptx: 'Slide', xml: 'No.', excel: null };

const CELL_BORDER: CSSProperties = { borderBottom: '1px solid var(--color-border)' };
const WRAP: CSSProperties = { ...CELL_BORDER, wordBreak: 'break-word' };

// Visual-change rows pair with images by order, not by page number: the LLM's page values are unreliable.
// The order is the one the images were sent in: orientation, modified, removed, added.
function pairImages(items: DiffItem[], images: ImagePair[]): (ImagePair | null)[] {
  let next = 0;
  return items.map(item => {
    const typeStr = (item.type || '').toLowerCase();
    const isImageRow = IMAGE_KEYWORDS.some(kw => typeStr.includes(kw));
    return images.length > 0 && isImageRow && next < images.length ? images[next++] : null;
  });
}

function Thumb({ b64, alt, onClick }: { b64: string; alt: string; onClick: () => void }) {
  return (
    <img
      src={`data:image/jpeg;base64,${b64}`}
      alt={alt}
      title={`Click to enlarge — ${alt}`}
      className="max-h-14 max-w-[54px] object-contain rounded border border-[var(--color-border)] cursor-pointer opacity-90 hover:opacity-100"
      onClick={onClick}
    />
  );
}

export function JsonDiffTable({ items, isStreaming, accentColor, fileType, imageContext }: {
  items: DiffItem[];
  isStreaming: boolean;
  accentColor: string;
  fileType: string;
  imageContext: ImagePair[];
}) {
  const [lightboxPair, setLightboxPair] = useState<ImagePair | null>(null);
  // Low-criticality rows are real changes reported on purpose; hidden by default so they do not drown out the rows that need action.
  const [hideLow, setHideLow] = useState(true);
  const [focusedChange, setFocusedChange] = useState<string | null>(null);
  const scrollRef = useStickToBottom<HTMLDivElement>(isStreaming, items.length, 80);

  useEffect(() => {
    const onFocus = (e: Event) => {
      setHideLow(false);
      setFocusedChange((e as CustomEvent<string>).detail);
    };
    window.addEventListener(FOCUS_CHANGE_EVENT, onFocus);
    return () => window.removeEventListener(FOCUS_CHANGE_EVENT, onFocus);
  }, []);

  useEffect(() => {
    if (!focusedChange) return;
    scrollRef.current
      ?.querySelector(`[data-change-id="${focusedChange}"]`)
      ?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    const timer = setTimeout(() => setFocusedChange(null), 2500);
    return () => clearTimeout(timer);
  }, [focusedChange, scrollRef]);

  const pairs = useMemo(() => pairImages(items, imageContext), [items, imageContext]);

  if (items.length === 0) {
    return isStreaming ? (
      <div className="flex items-center gap-2 text-[var(--color-text-muted)] p-5">
        <Loader2 className="h-4 w-4 animate-spin" style={{ color: accentColor }} />
        <span className="italic text-sm">Generating structured analysis…</span>
      </div>
    ) : (
      <div className="px-5 py-4 text-sm italic text-[var(--color-text-muted)]">No significant changes detected.</div>
    );
  }

  const hasImages = imageContext.length > 0;
  const pageLabel = fileType in PAGE_LABEL ? PAGE_LABEL[fileType] : 'Page';
  const lowCount = items.filter(it => (it.criticality || '').toLowerCase() === 'low').length;
  // Row number = the change id used by the impact search (C1, C2…).
  const headers = ['#', 'Section', ...(pageLabel ? [pageLabel] : []), 'Type', 'Criticality', 'Before', 'After', 'Rationale', ...(hasImages ? ['Image'] : [])];

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
      <div ref={scrollRef} className="overflow-auto max-h-[650px] px-5 py-3">
        <table className="w-full text-xs border-collapse" style={{ minWidth: hasImages ? 820 : 700 }}>
          <thead>
            <tr style={{ background: 'var(--color-muted)' }}>
              {headers.map(h => (
                <th key={h} className="px-2 py-1.5 text-left font-semibold whitespace-nowrap" style={{ color: 'var(--color-text-heading)', ...CELL_BORDER }}>
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {items.map((item, i) => {
              const critKey = (item.criticality || '').toLowerCase();
              if (hideLow && critKey === 'low') return null;
              const crit = CRIT_STYLE[critKey] ?? { color: 'var(--color-text-body)', badge: 'transparent' };
              const pair = pairs[i];
              const id = `C${i + 1}`;
              return (
                <tr
                  key={i}
                  data-change-id={id}
                  style={{
                    background: focusedChange === id ? '#fde68a66' : i % 2 === 0 ? 'transparent' : 'var(--color-muted)',
                    transition: 'background 0.4s',
                  }}
                >
                  <td className="px-2 py-1.5 align-top font-mono whitespace-nowrap" style={{ ...CELL_BORDER, color: 'var(--color-text-muted)' }}>{id}</td>
                  <td className="px-2 py-1.5 align-top font-medium" style={{ ...WRAP, maxWidth: 140 }}>{item.section || '—'}</td>
                  {pageLabel && <td className="px-2 py-1.5 align-top whitespace-nowrap text-center" style={CELL_BORDER}>{item.page || '—'}</td>}
                  <td className="px-2 py-1.5 align-top whitespace-nowrap" style={CELL_BORDER}>{item.type || '—'}</td>
                  <td className="px-2 py-1.5 align-top" style={CELL_BORDER}>
                    {item.criticality ? (
                      <span className="px-1.5 py-0.5 rounded text-xs font-semibold" style={{ color: crit.color, background: crit.badge }}>
                        {item.criticality}
                      </span>
                    ) : '—'}
                  </td>
                  <td className="px-2 py-1.5 align-top" style={{ ...WRAP, maxWidth: 200 }}>{item.before || '—'}</td>
                  <td className="px-2 py-1.5 align-top" style={{ ...WRAP, maxWidth: 200 }}>{item.after || '—'}</td>
                  <td className="px-2 py-1.5 align-top" style={{ ...WRAP, maxWidth: 220 }}>{item.rationale || '—'}</td>
                  {hasImages && (
                    <td className="px-2 py-1 align-middle" style={{ ...CELL_BORDER, width: 120 }}>
                      {pair ? (
                        <div className="flex gap-1 items-center justify-center">
                          {pair.old_b64 && <Thumb b64={pair.old_b64} alt="Before" onClick={() => setLightboxPair(pair)} />}
                          {pair.new_b64 && <Thumb b64={pair.new_b64} alt="After" onClick={() => setLightboxPair(pair)} />}
                        </div>
                      ) : '—'}
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {lightboxPair && <ImageLightbox pair={lightboxPair} onClose={() => setLightboxPair(null)} />}
    </div>
  );
}
