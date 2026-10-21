import { AlertTriangle } from 'lucide-react';

/** Shown when the user's permissions could not be read; access is never assumed. */
export function AccessCheckFailed({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="flex flex-col items-center justify-center h-full gap-4 px-6 text-center">
      <AlertTriangle className="h-8 w-8" style={{ color: 'var(--color-text-muted)' }} />
      <h2
        className="text-xl font-semibold"
        style={{ color: 'var(--color-text-primary)', fontFamily: 'var(--font-heading)' }}
      >
        Could not check your access
      </h2>
      <p className="text-sm max-w-sm" style={{ color: 'var(--color-text-muted)' }}>
        Your permissions could not be loaded. Check your connection and try again.
      </p>
      <button
        onClick={onRetry}
        className="px-4 py-2 rounded-md text-sm font-medium border border-[var(--color-border)] hover:bg-[var(--color-muted)]"
      >
        Retry
      </button>
    </div>
  );
}
