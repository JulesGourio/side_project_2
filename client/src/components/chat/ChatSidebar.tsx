import { MessageSquarePlus, Trash2, Clock } from 'lucide-react';
import { DivisionSelector, type Division } from './division';

export interface ChatSession {
  id: string;
  name: string;
  created_at: string;
  updated_at: string;
  message_count: number;
}

interface ChatSidebarProps {
  sessions: ChatSession[];
  currentSessionId: string | null;
  onSelectSession: (id: string) => void;
  onNewChat: () => void;
  onDeleteSession: (id: string) => void;
  loading: boolean;
  division: Division;
  onDivisionChange: (d: Division) => void;
}

// Calendar days in the viewer's time zone, not 24-hour periods: a question asked yesterday evening is
// "Yesterday" this morning even though fewer than 24 hours have passed.
export function formatRelativeDate(isoString: string, now: Date = new Date()): string {
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) return '';
  const startOfDay = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const diffDays = Math.round((startOfDay(now) - startOfDay(date)) / 86400000);

  if (diffDays <= 0) return 'Today';
  if (diffDays === 1) return 'Yesterday';
  if (diffDays < 7) return `${diffDays} days ago`;
  return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

export function ChatSidebar({
  sessions,
  currentSessionId,
  onSelectSession,
  onNewChat,
  onDeleteSession,
  loading,
  division,
  onDivisionChange,
}: ChatSidebarProps) {
  return (
    <div
      className="flex flex-col w-60 flex-shrink-0 border-r h-full"
      style={{ borderColor: 'var(--color-border)', background: 'var(--color-bg-secondary)' }}
    >
      {/* New chat button */}
      <div className="p-3 border-b flex-shrink-0" style={{ borderColor: 'var(--color-border)' }}>
        <button
          onClick={onNewChat}
          className="w-full flex items-center gap-2 px-3 py-2 rounded-xl text-sm font-medium transition-all cursor-pointer"
          style={{
            background: 'linear-gradient(135deg, var(--color-accent-primary) 0%, var(--color-accent-secondary) 100%)',
            color: '#fff',
          }}
        >
          <MessageSquarePlus className="h-4 w-4 flex-shrink-0" />
          New conversation
        </button>

        {/* Division scope selector */}
        <DivisionSelector value={division} onChange={onDivisionChange} />
      </div>

      {/* Session list */}
      <div className="flex-1 overflow-y-auto py-2">
        {loading ? (
          <div className="px-3 py-4 text-center text-xs" style={{ color: 'var(--color-text-muted)' }}>
            Loading…
          </div>
        ) : sessions.length === 0 ? (
          <div className="px-3 py-4 text-center space-y-1">
            <Clock className="h-5 w-5 mx-auto opacity-30" style={{ color: 'var(--color-text-muted)' }} />
            <p className="text-xs" style={{ color: 'var(--color-text-muted)' }}>No conversations yet</p>
          </div>
        ) : (
          <>
            {sessions.map(session => {
              const isActive = session.id === currentSessionId;
              return (
                <div
                  key={session.id}
                  className="group relative mx-2 mb-0.5 rounded-xl cursor-pointer transition-all"
                  style={{
                    background: isActive ? 'var(--color-bg-tertiary)' : 'transparent',
                  }}
                  onClick={() => onSelectSession(session.id)}
                  onMouseEnter={e => {
                    if (!isActive) (e.currentTarget as HTMLDivElement).style.background = 'var(--color-bg-tertiary)';
                  }}
                  onMouseLeave={e => {
                    if (!isActive) (e.currentTarget as HTMLDivElement).style.background = 'transparent';
                  }}
                >
                  <div className="px-3 py-2 pr-8">
                    <p
                      className="text-xs font-medium truncate leading-snug"
                      style={{ color: isActive ? 'var(--color-text-heading)' : 'var(--color-text-primary)' }}
                    >
                      {session.name || 'Conversation'}
                    </p>
                    <p className="text-xs mt-0.5" style={{ color: 'var(--color-text-muted)' }}>
                      {formatRelativeDate(session.updated_at)}
                    </p>
                  </div>

                  {/* Delete button */}
                  <button
                    onClick={e => { e.stopPropagation(); onDeleteSession(session.id); }}
                    className="absolute right-2 top-1/2 -translate-y-1/2 w-6 h-6 rounded-lg items-center justify-center opacity-0 group-hover:opacity-100 transition-all cursor-pointer hidden group-hover:flex"
                    style={{ color: 'var(--color-text-muted)' }}
                    title="Delete conversation"
                    onMouseEnter={e => (e.currentTarget.style.color = 'var(--color-error)')}
                    onMouseLeave={e => (e.currentTarget.style.color = 'var(--color-text-muted)')}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
              );
            })}
          </>
        )}
      </div>
    </div>
  );
}
