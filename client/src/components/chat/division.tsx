// The backend filters the document search on the `division` column, so the scope is
// enforced by the filter rather than by a directive in the question.

export type Division = 'ALL' | 'AS' | 'IS';

const DIVISIONS: { value: Division; label: string; title: string }[] = [
  { value: 'ALL', label: 'All', title: 'Search across every division' },
  { value: 'AS', label: 'AS', title: 'Aerostructures — search the AS / shared documents only' },
  { value: 'IS', label: 'IS', title: 'Interconnection — search the IS / shared documents only' },
];

// A stored question may start with a "[Division: …]" prefix, which is not shown to the user.
const DIVISION_PREFIX_RE = /^\[Division: (?:AS|IS)\][\s\S]*?\n\n/;

export function stripDivision(content: string): string {
  return content.replace(DIVISION_PREFIX_RE, '');
}

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
