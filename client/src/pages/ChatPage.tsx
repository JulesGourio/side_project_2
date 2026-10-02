import { useEffect, useState } from 'react';
import { ChatView } from '@/components/chat/ChatView';
import { AccessDenied } from '@/components/shared/AccessDenied';
import { getAppConfig, getUserMe } from '@/lib/config';

export function ChatPage() {
  const [state, setState] = useState<{ chatEnabled: boolean; canChat: boolean } | null>(null);

  useEffect(() => {
    Promise.all([getAppConfig(), getUserMe()]).then(([cfg, me]) =>
      setState({ chatEnabled: cfg.chat?.enabled ?? true, canChat: me.can_chat }),
    );
  }, []);

  if (state === null) return null;

  if (!state.canChat) return <AccessDenied feature="Chat" />;

  if (!state.chatEnabled) {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-4 px-6 text-center">
        <div
          className="w-16 h-16 rounded-2xl flex items-center justify-center mb-2"
          style={{ background: 'rgba(77,163,232,0.12)', border: '1px solid rgba(77,163,232,0.25)' }}
        >
          <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="rgba(77,163,232,0.7)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
          </svg>
        </div>
        <h2
          className="text-xl font-semibold"
          style={{ color: 'var(--color-text-primary)', fontFamily: 'var(--font-heading)' }}
        >
          Chat — Coming soon
        </h2>
        <p className="text-sm max-w-sm" style={{ color: 'var(--color-text-muted)' }}>
          A conversational assistant to explore your document knowledge base will be available here.
        </p>
      </div>
    );
  }

  return <ChatView />;
}
