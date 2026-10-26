import { MessageSquare } from 'lucide-react';

export function WelcomeState({ title }: { title: string }) {
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

      <div className="max-w-xl mt-6 px-4 text-center">
        <p className="text-xs leading-relaxed" style={{ color: 'var(--color-text-muted)', opacity: 0.85 }}>
          <strong>Disclaimer:</strong> To help improve QualiBot and ensure its proper operation, user prompts may be logged and reviewed by authorized members of the Data & AI team. These logs are used exclusively for monitoring, troubleshooting, anomaly detection, and service improvement.
        </p>
      </div>
    </div>
  );
}
