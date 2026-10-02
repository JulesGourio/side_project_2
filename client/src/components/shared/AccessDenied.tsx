import { Lock } from 'lucide-react';

interface AccessDeniedProps {
  /** Feature name used to build a default message (e.g. "Chat"). */
  feature?: string;
  /** Override the heading. */
  title?: string;
  /** Override the body message. */
  message?: string;
}

/**
 * Shown when a user opens a feature they are not entitled to. The tab stays
 * visible (see TopBar), but the feature itself is replaced by this panel — and
 * the backend independently returns 403, so it cannot be bypassed.
 */
export function AccessDenied({ feature, title, message }: AccessDeniedProps) {
  const heading = title ?? `${feature} — access not granted`;
  const body =
    message ??
    `You don't have permission to use ${(feature ?? 'this feature').toLowerCase()}. ` +
      `Contact your administrator to request access.`;

  return (
    <div className="flex flex-col items-center justify-center h-full gap-4 px-6 text-center">
      <div
        className="w-16 h-16 rounded-2xl flex items-center justify-center mb-2"
        style={{ background: 'rgba(77,163,232,0.12)', border: '1px solid rgba(77,163,232,0.25)' }}
      >
        <Lock className="h-7 w-7" style={{ color: 'rgba(77,163,232,0.7)' }} />
      </div>
      <h2
        className="text-xl font-semibold"
        style={{ color: 'var(--color-text-primary)', fontFamily: 'var(--font-heading)' }}
      >
        {heading}
      </h2>
      <p className="text-sm max-w-sm" style={{ color: 'var(--color-text-muted)' }}>
        {body}
      </p>
    </div>
  );
}
