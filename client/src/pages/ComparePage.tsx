import { useEffect, useState } from 'react';
import { CompareView } from '@/components/compare/CompareView';
import { AccessDenied } from '@/components/shared/AccessDenied';
import { getUserMe } from '@/lib/config';

export function ComparePage() {
  const [canCompare, setCanCompare] = useState<boolean | null>(null);

  useEffect(() => {
    getUserMe().then((me) => setCanCompare(me.can_compare));
  }, []);

  if (canCompare === null) return null;
  if (!canCompare) return <AccessDenied feature="Document comparison" />;

  return <CompareView />;
}
