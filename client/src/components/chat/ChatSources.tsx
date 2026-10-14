import { FileText } from 'lucide-react';
import { Flag } from '@/components/shared/Flag';
import { versionLabel, type Source } from './message';

// Mirrors doc_catalog.py's _canon(): same document, different intraqual
// site/language suffix (GO-1316_FR / GO-1316_GB / PRLAT-529 / PRLAT-529_GB /
// PRLAT536_FR...). Used only to group chips for display — matching/lookup
// already happened server-side.
const LANG_SUFFIX_RE = /[-_. ]+(FR|EN|GB|MX|BG|CZ|BR|ES)$/i;

function langCode(title: string): string | null {
  const m = title.match(LANG_SUFFIX_RE);
  return m ? m[1].toUpperCase() : null;
}

function canonicalDocKey(title: string): string {
  return title.toUpperCase().replace(LANG_SUFFIX_RE, '').replace(/[^A-Z0-9]/g, '');
}

// Fixed display order — NOT a claim about which variant is "the" REF.
// Intraqual has no reliable default language (some documents' undecorated
// REF is the French original, others' is the English one), so the group
// label is the shared document code (suffix stripped), not one language's
// title — see SourceChipGroup.
const LANG_ORDER = ['FR', 'EN', 'GB', 'ES', 'MX', 'BG', 'CZ', 'BR'];

// The inline-cited variant (if any) leads the row — that replaces the old
// numbered badge as the "which one did the model actually cite" signal.
function sortVariants(variants: Source[]): Source[] {
  return [...variants].sort((a, b) => {
    if ((a.n != null) !== (b.n != null)) return a.n != null ? -1 : 1;
    const ca = langCode(a.title || '');
    const cb = langCode(b.title || '');
    if (!ca || !cb) return (ca ? 0 : 1) - (cb ? 0 : 1);
    return LANG_ORDER.indexOf(ca) - LANG_ORDER.indexOf(cb);
  });
}

const chipBase = 'group inline-flex items-center gap-1.5 max-w-[260px] py-1 rounded-full border text-xs transition-all';
const chipStyle = { borderColor: 'var(--color-border)', background: 'var(--color-bg-secondary)' };

function chipNumberBadge(n: number) {
  return (
    <span
      className="flex items-center justify-center h-4 w-4 rounded-full text-[10px] font-semibold flex-shrink-0"
      style={{ background: 'var(--color-accent-primary)', color: '#fff' }}
    >
      {n}
    </span>
  );
}

// A single, un-grouped source: the plain pill.
// Hover text of a chip: REF — title, version, link.
function chipTooltip(src: Source, label: string): string {
  return [src.doc_title ? `${label} — ${src.doc_title}` : label, versionLabel(src), src.url !== label ? src.url : '']
    .filter(Boolean).join('\n');
}

function SourceChip({ src, fallbackLabel }: { src: Source; fallbackLabel: string }) {
  const label = src.title || src.url || fallbackLabel;
  const pad = src.n != null ? 'pl-1 pr-2.5' : 'px-2.5';
  const text = (
    <span className="truncate font-medium" style={{ color: 'var(--color-text-primary)' }}>
      {label}
      {src.revision && (
        <span className="font-normal ml-1" style={{ color: 'var(--color-text-muted)' }}>· {src.revision}</span>
      )}
    </span>
  );

  return src.url ? (
    <a
      href={src.url}
      target="_blank"
      rel="noopener noreferrer"
      title={chipTooltip(src, label)}
      className={`${chipBase} ${pad} cursor-pointer`}
      style={chipStyle}
      onMouseEnter={e => (e.currentTarget.style.borderColor = 'var(--color-accent-primary)')}
      onMouseLeave={e => (e.currentTarget.style.borderColor = 'var(--color-border)')}
    >
      {src.n != null && chipNumberBadge(src.n)}
      {text}
    </a>
  ) : (
    <span title={chipTooltip(src, label)} className={`${chipBase} ${pad}`} style={chipStyle}>
      {src.n != null && chipNumberBadge(src.n)}
      {text}
    </span>
  );
}

// A document with 2+ site/language variants: one chip showing the shared
// document code (the REF with its language suffix stripped — the same code
// for every variant, so it doesn't privilege whichever language happens to
// be the "bare" one) plus a small flag per available language. Clicking the
// code opens the bare/undecorated variant if there is one; each flag opens
// its own language's document. Full REFs are in the chip's tooltip.
function SourceChipGroup({ variants }: { variants: Source[] }) {
  const bare = variants.find(v => !langCode(v.title || ''));
  const label = (variants[0]?.title || variants[0]?.url || 'Source').replace(LANG_SUFFIX_RE, '');
  const flagVariants = sortVariants(variants).filter(v => v !== bare);
  const tooltip = variants.map(v => chipTooltip(v, v.title)).filter(Boolean).join('\n\n');

  return (
    <span className={`${chipBase} px-2.5`} style={chipStyle} title={tooltip}>
      {bare?.url ? (
        <a
          href={bare.url}
          target="_blank"
          rel="noopener noreferrer"
          className="truncate font-medium hover:underline"
          style={{ color: 'var(--color-text-primary)' }}
        >
          {label}
        </a>
      ) : (
        <span className="truncate font-medium opacity-40" style={{ color: 'var(--color-text-primary)' }}>
          {label}
        </span>
      )}
      <span className="flex items-center gap-1 pl-1.5 ml-0.5 border-l flex-shrink-0" style={{ borderColor: 'var(--color-border)' }}>
        {flagVariants.map((v, j) => {
          const code = langCode(v.title || '');
          if (!code) return null;
          return v.url ? (
            <a key={j} href={v.url} target="_blank" rel="noopener noreferrer" title={v.title} className="transition-transform hover:scale-125">
              <Flag code={code} />
            </a>
          ) : (
            <span key={j} title={v.title} className="opacity-40">
              <Flag code={code} />
            </span>
          );
        })}
      </span>
    </span>
  );
}

// Clickable source chips — each consulted document is a small pill showing its
// REF (reference code). Sources cited inline carry a small number badge that
// matches their [n] marker in the answer; prose-only sources re-surfaced from
// the catalog have no number and show just the REF. Chips with a URL open it in
// a new tab; chips without one are a plain badge (full ref on hover). Documents
// with several intraqual site/language variants (see canonicalDocKey) collapse
// into a single chip with per-language badges instead of one chip each.
export function ChatSources({ sources }: { sources: Source[] }) {
  if (!sources.length) return null;

  const groups: Source[][] = [];
  const indexByKey = new Map<string, number>();
  for (const src of sources) {
    const key = canonicalDocKey(src.title || src.url || '');
    const idx = indexByKey.get(key);
    if (idx === undefined) {
      indexByKey.set(key, groups.length);
      groups.push([src]);
    } else {
      groups[idx].push(src);
    }
  }

  return (
    <div className="mt-2 flex flex-wrap items-center gap-1.5">
      <span
        className="inline-flex items-center gap-1 text-xs font-medium mr-0.5"
        style={{ color: 'var(--color-text-muted)' }}
      >
        <FileText className="h-3.5 w-3.5 flex-shrink-0" />
        Sources&nbsp;:
      </span>

      {groups.map((variants, i) =>
        variants.length > 1 ? (
          <SourceChipGroup key={i} variants={variants} />
        ) : (
          <SourceChip key={i} src={variants[0]} fallbackLabel={`Source ${i + 1}`} />
        )
      )}
    </div>
  );
}
