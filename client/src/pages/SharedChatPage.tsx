import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { Copy, MessageSquareOff } from 'lucide-react';
import { toast } from 'sonner';
import { ChatMessage, type Message } from '@/components/chat/ChatMessage';

interface SharedSession {
  id: string;
  name: string;
  messages: Message[];
}

function NotFound() {
  return (
    <div className="flex flex-col items-center justify-center h-full gap-4 px-6 text-center">
      <div
        className="w-16 h-16 rounded-2xl flex items-center justify-center mb-2"
        style={{ background: 'rgba(77,163,232,0.12)', border: '1px solid rgba(77,163,232,0.25)' }}
      >
        <MessageSquareOff className="h-7 w-7" style={{ color: 'rgba(77,163,232,0.7)' }} />
      </div>
      <h2
        className="text-xl font-semibold"
        style={{ color: 'var(--color-text-primary)', fontFamily: 'var(--font-heading)' }}
      >
        Conversation not found
      </h2>
      <p className="text-sm max-w-sm" style={{ color: 'var(--color-text-muted)' }}>
        This share link is no longer valid, or the conversation was deleted by its owner.
      </p>
    </div>
  );
}

// Read-only view of a shared conversation — anyone with the link can open it
// (no ownership check, see GET /api/chat/shared/{token}). The owner's own
// vote/comment is displayed (showFeedback=false still renders it read-only in
// ChatMessage), but a viewer can never cast a new vote — that would pollute
// the owner's own triage signal.
export function SharedChatPage() {
  const { token } = useParams<{ token: string }>();
  const navigate = useNavigate();
  const [session, setSession] = useState<SharedSession | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [duplicating, setDuplicating] = useState(false);

  useEffect(() => {
    if (!token) return;
    fetch(`/api/chat/shared/${token}`)
      .then(res => { if (!res.ok) throw new Error(); return res.json(); })
      .then((data: { id: string; name: string; messages: (Message & { id: number })[] }) => {
        setSession({
          id: data.id,
          name: data.name,
          messages: data.messages.map(m => ({ ...m, id: String(m.id) })),
        });
      })
      .catch(() => setNotFound(true));
  }, [token]);

  const handleDuplicate = async () => {
    if (!token || duplicating) return;
    setDuplicating(true);
    try {
      const res = await fetch(`/api/chat/shared/${token}/duplicate`, { method: 'POST' });
      if (!res.ok) throw new Error();
      const data: { session_id: string } = await res.json();
      navigate(`/chat?session=${data.session_id}`);
    } catch {
      toast.error('Failed to duplicate this conversation');
      setDuplicating(false);
    }
  };

  if (notFound) return <NotFound />;
  if (!session) return null;

  return (
    <div className="flex flex-col h-full overflow-hidden">
      <div
        className="flex items-center gap-2 px-4 py-2 border-b flex-shrink-0"
        style={{ borderColor: 'var(--color-border)', background: 'var(--color-bg-primary)' }}
      >
        <span
          className="text-sm font-semibold truncate"
          style={{ color: 'var(--color-text-heading)', fontFamily: 'var(--font-heading)' }}
        >
          {session.name || 'Shared conversation'}
        </span>
        <span
          className="text-xs px-2 py-0.5 rounded-full flex-shrink-0"
          style={{ background: 'var(--color-bg-tertiary)', color: 'var(--color-text-muted)' }}
        >
          Read-only
        </span>
        <div className="flex-1" />
        <button
          onClick={handleDuplicate}
          disabled={duplicating}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-semibold text-white transition-all disabled:opacity-50 cursor-pointer flex-shrink-0"
          style={{ background: 'linear-gradient(135deg, var(--color-accent-primary) 0%, var(--color-accent-secondary) 100%)' }}
        >
          <Copy className="h-3.5 w-3.5" />
          {duplicating ? 'Duplicating…' : 'Duplicate into my conversations'}
        </button>
      </div>

      <div className="flex-1 overflow-y-auto py-4">
        {session.messages.map(msg => (
          <ChatMessage key={msg.id} message={msg} showFeedback={false} allMessages={session.messages} />
        ))}
      </div>
    </div>
  );
}
