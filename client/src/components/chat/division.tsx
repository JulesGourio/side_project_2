// Division scope — shared type, helpers and selector for the chat.
//
// The user picks a division (ALL / AS / IS). The choice is sent to the backend,
// which filters the document search on it (Vector Search filter on the
// `division` column) and picks the division's instructions — the scope is
// guaranteed by the filter, not by a directive in the question.

export type Division = 'ALL' | 'AS' | 'IS';

export const DIVISIONS: { value: Division; label: string; title: string }[] = [
  { value: 'ALL', label: 'All', title: 'Search across every division' },
  { value: 'AS', label: 'AS', title: 'Aerostructures — search the AS / shared documents only' },
  { value: 'IS', label: 'IS', title: 'Interconnection — search the IS / shared documents only' },
];

// Legacy turns persisted before endpoint routing still carry a "[Division: …]"
// directive at the start of the stored question. Strip it when re-displaying so
// the user sees only their original question.
const DIVISION_PREFIX_RE = /^\[Division: (?:AS|IS)\][\s\S]*?\n\n/;

/** Remove the legacy division directive so the user sees only their question. */
export function stripDivision(content: string): string {
  return content.replace(DIVISION_PREFIX_RE, '');
}

// --- Selector ---

interface DivisionSelectorProps {
  value: Division;
  onChange: (d: Division) => void;
}

export function DivisionSelector({ value, onChange }: DivisionSelectorProps) {
  return (
    <div className="mt-2">
      <span
        className="block mb-1 text-[11px] font-medium uppercase tracking-wide"
        style={{ color: 'var(--color-text-muted)' }}
      >
        Division
      </span>
      <div
        className="flex items-center gap-1 rounded-lg p-0.5"
        style={{ background: 'var(--color-bg-primary)', border: '1px solid var(--color-border)' }}
        title="Restrict the assistant's search to a division"
      >
        {DIVISIONS.map(d => {
          const active = value === d.value;
          return (
            <button
              key={d.value}
              onClick={() => onChange(d.value)}
              title={d.title}
              className="flex-1 px-2 py-1 rounded-md text-xs font-medium transition-all cursor-pointer"
              style={{
                background: active
                  ? 'linear-gradient(135deg, var(--color-accent-primary) 0%, var(--color-accent-secondary) 100%)'
                  : 'transparent',
                color: active ? '#fff' : 'var(--color-text-muted)',
              }}
            >
              {d.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}
