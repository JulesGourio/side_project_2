import { CompareView } from '@/components/compare/CompareView';
import { AccessCheckFailed } from '@/components/shared/AccessCheckFailed';
import { AccessDenied } from '@/components/shared/AccessDenied';
import { useUserMe } from '@/lib/useUserMe';

export function ComparePage() {
  const { state, retry } = useUserMe();

  if (state.status === 'loading') return null;
  if (state.status === 'failed') return <AccessCheckFailed onRetry={retry} />;
  if (!state.canCompare) return <AccessDenied feature="Document comparison" />;

  return <CompareView />;
}
