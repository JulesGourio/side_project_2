import { useCallback, useEffect, useState } from 'react';
import { toast } from 'sonner';
import type { ChatSession } from './ChatSidebar';

/** The user's saved conversations (sidebar history). */
export function useChatSessions() {
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [available, setAvailable] = useState(false);
  const [loading, setLoading] = useState(true);

  const reload = useCallback(async () => {
    try {
      const res = await fetch('/api/chat/sessions');
      if (!res.ok) return;
      const data = await res.json();
      setSessions(data.sessions ?? []);
      setAvailable(data.available ?? false);
    } catch {
      // DB not available — hide sidebar history
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { reload(); }, [reload]);

  /** Returns whether the conversation was deleted. */
  const remove = useCallback(async (id: string): Promise<boolean> => {
    try {
      await fetch(`/api/chat/sessions/${id}`, { method: 'DELETE' });
      setSessions(prev => prev.filter(s => s.id !== id));
      return true;
    } catch {
      toast.error('Failed to delete conversation');
      return false;
    }
  }, []);

  return { sessions, available, loading, reload, remove };
}
