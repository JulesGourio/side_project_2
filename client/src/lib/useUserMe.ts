import { useCallback, useEffect, useState } from 'react';
import { getUserMe } from './config';

type UserMeState = { status: 'loading' } | { status: 'failed' } | { status: 'ready'; canCompare: boolean; canChat: boolean };

/** The signed-in user's permissions; `retry` reads them again after a failure. */
export function useUserMe(): { state: UserMeState; retry: () => void } {
  const [state, setState] = useState<UserMeState>({ status: 'loading' });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let active = true;
    setState({ status: 'loading' });
    getUserMe().then(
      (me) => { if (active) setState({ status: 'ready', canCompare: me.can_compare, canChat: me.can_chat }); },
      () => { if (active) setState({ status: 'failed' }); },
    );
    return () => { active = false; };
  }, [attempt]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);
  return { state, retry };
}
