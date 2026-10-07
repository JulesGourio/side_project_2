import { ChatPage } from '@/pages/ChatPage';

// Chat VSI tab: the same chat as the Chat KA tab (access control, sessions, sources),
// answered by the Vector Search engine through /api/chat-vsi/ws.
export function ChatVsiPage() {
  return <ChatPage engine="vsi" />;
}
