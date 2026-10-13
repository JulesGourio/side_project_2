import { MessageSquare } from 'lucide-react';

const SUGGESTED_PROMPTS = [
  'What documents reference the NDT/NDI qualification requirements?',
  'Which procedures must be updated when a supplier changes their process?',
  'List the key quality standards applicable to composite part manufacturing.',
  'What is the approval process for deviations from engineering specifications?',
];

export function WelcomeState({ title, onPrompt }: { title: string; onPrompt: (p: string) => void }) {
  return (
    <div className="flex flex-col items-center justify-center h-full gap-8 px-6">
      <div className="text-center space-y-4 max-w-lg">
        <div
          className="w-16 h-16 rounded-2xl flex items-center justify-center mx-auto shadow-sm"
          style={{ background: 'var(--color-accent-primary)20' }}
        >
          <MessageSquare className="h-8 w-8" style={{ color: 'var(--color-accent-primary)' }} />
        </div>
        <h2 className="text-3xl font-bold tracking-tight" style={{ color: 'var(--color-text-heading)', fontFamily: 'var(--font-heading)' }}>
          {title}
        </h2>
        <p className="text-base" style={{ color: 'var(--color-text-muted)' }}>
          Ask questions about your document knowledge base (Intraqual). The assistant can retrieve relevant
          information and identify impacted documents.
        </p>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5 w-full max-w-lg mt-2">
        {SUGGESTED_PROMPTS.map(prompt => (
          <button
            key={prompt}
            onClick={() => onPrompt(prompt)}
            className="text-left px-4 py-3 rounded-xl border text-sm leading-snug transition-all cursor-pointer border-[var(--color-border)] text-[var(--color-text-primary)] bg-[var(--color-bg-secondary)] hover:border-[var(--color-accent-primary)] hover:bg-[var(--color-bg-tertiary)]"
          >
            {prompt}
          </button>
        ))}
      </div>

      <div className="max-w-xl mt-6 px-4 text-center">
        <p className="text-xs leading-relaxed" style={{ color: 'var(--color-text-muted)', opacity: 0.85 }}>
          <strong>Disclaimer:</strong> To help improve QualiBot and ensure its proper operation, user prompts may be logged and reviewed by authorized members of the Data & AI team. These logs are used exclusively for monitoring, troubleshooting, anomaly detection, and service improvement.
        </p>
      </div>
    </div>
  );
}
