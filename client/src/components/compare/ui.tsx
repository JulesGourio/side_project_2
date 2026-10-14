import { useEffect, useRef, type ButtonHTMLAttributes, type CSSProperties, type ElementType, type ReactNode } from 'react';
import { Download, Loader2, Square, X } from 'lucide-react';

export const ACCENT = {
  blue: 'var(--color-accent-primary)',
  amber: '#d97706',
  green: '#16a34a',
} as const;

const BUTTON_BASE = 'flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-semibold transition-all duration-150 border cursor-pointer disabled:opacity-40';

export function OutlineButton({ className = '', ...props }: ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      {...props}
      className={`${BUTTON_BASE} border-[var(--color-border)] text-[var(--color-text-muted)] hover:border-[var(--color-accent-primary)] hover:text-[var(--color-accent-primary)] ${className}`}
    />
  );
}

export function AccentButton({ accent, style, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { accent: string }) {
  return <button {...props} className={BUTTON_BASE} style={{ borderColor: accent, color: accent, background: 'transparent', ...style }} />;
}

export function CancelButton({ onClick }: { onClick: () => void }) {
  return (
    <button onClick={onClick} className={`${BUTTON_BASE} border-[var(--color-border)]`}>
      <Square className="h-3.5 w-3.5" />
      Cancel
    </button>
  );
}

export function TabSwitch<T extends string>({ tabs, active, accent, onChange }: {
  tabs: readonly (readonly [T, string])[];
  active: T;
  accent: string;
  onChange: (tab: T) => void;
}) {
  return (
    <div className="inline-flex rounded-xl border border-[var(--color-border)]/40 p-1 gap-1">
      {tabs.map(([id, label]) => (
        <button
          key={id}
          onClick={() => onChange(id)}
          className="px-3.5 py-1.5 rounded-lg text-sm font-medium transition-all cursor-pointer"
          style={active === id ? { background: accent, color: 'white' } : { color: 'var(--color-text-muted)', background: 'transparent' }}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

export function Spinner({ accent, className = 'h-4 w-4' }: { accent: string; className?: string }) {
  return <Loader2 className={`${className} animate-spin`} style={{ color: accent }} />;
}

export function GeneratingBadge({ accent, label = 'Generating…' }: { accent: string; label?: string }) {
  return (
    <span className="flex-shrink-0 flex items-center gap-1.5 text-xs text-[var(--color-text-muted)]">
      <Spinner accent={accent} className="h-3 w-3" />
      {label}
    </span>
  );
}

export function DownloadButton({ accent, label, title, onClick }: { accent: string; label: string; title: string; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-all cursor-pointer hover:bg-[color-mix(in_srgb,var(--accent)_7%,transparent)]"
      style={{ color: accent, ['--accent' as string]: accent }}
      title={title}
    >
      <Download className="h-3.5 w-3.5" />
      {label}
    </button>
  );
}

export function CardShell({ icon: Icon, title, accent, status, actions, children }: {
  icon: ElementType;
  title: string;
  accent: string;
  status?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="rounded-2xl border border-[var(--color-border)]/40 bg-[var(--color-background)] shadow-sm overflow-hidden">
      <div className="h-0.5 w-full" style={{ background: accent }} />
      <div className="flex items-center justify-between px-5 py-4 border-b border-[var(--color-border)]/30 bg-[var(--color-bg-secondary)]/40 gap-3">
        <div className="flex items-center gap-2.5 min-w-0 flex-1">
          <div
            className="flex-shrink-0 w-7 h-7 rounded-lg flex items-center justify-center"
            style={{ background: `color-mix(in srgb, ${accent} 9%, transparent)` }}
          >
            <Icon className="h-3.5 w-3.5" style={{ color: accent }} />
          </div>
          <span className="text-sm font-semibold text-[var(--color-text-heading)] truncate">{title}</span>
          {status}
        </div>
        {actions && <div className="flex-shrink-0 flex items-center gap-3">{actions}</div>}
      </div>
      {children}
    </div>
  );
}

export function ModalShell({ title, onClose, backdropClassName = '', panelStyle, headerClassName, children }: {
  title: ReactNode;
  onClose: () => void;
  backdropClassName?: string;
  panelStyle: CSSProperties;
  headerClassName: string;
  children: ReactNode;
}) {
  return (
    <div className={`fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm ${backdropClassName}`} onClick={onClose}>
      <div
        className="relative flex flex-col rounded-2xl shadow-2xl overflow-hidden"
        style={{ background: 'var(--color-background)', ...panelStyle }}
        onClick={e => e.stopPropagation()}
      >
        <div className={`flex items-center justify-between border-b border-[var(--color-border)] ${headerClassName}`}>
          <span className="text-sm font-semibold truncate text-[var(--color-text-heading)]">{title}</span>
          <button
            onClick={onClose}
            className="w-7 h-7 rounded-lg flex items-center justify-center transition-all cursor-pointer text-[var(--color-text-muted)] hover:text-[var(--color-error)]"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

export function useStickToBottom<T extends HTMLElement>(streaming: boolean, trigger: unknown, threshold: number) {
  const ref = useRef<T>(null);
  useEffect(() => {
    const el = ref.current;
    if (!streaming || !el) return;
    if (el.scrollHeight - el.scrollTop - el.clientHeight < threshold) el.scrollTop = el.scrollHeight;
  }, [streaming, trigger, threshold]);
  return ref;
}
