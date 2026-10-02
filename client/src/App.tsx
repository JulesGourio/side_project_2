import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { useEffect, useState } from 'react';
import { CustomThemeProvider } from '@/contexts/ThemeContext';
import { TopBar } from '@/components/layout/TopBar';
import { ComparePage } from '@/pages/ComparePage';
import { ChatPage } from '@/pages/ChatPage';
import { SharedChatPage } from '@/pages/SharedChatPage';
import { AccessDenied } from '@/components/shared/AccessDenied';
import { getAppConfig, getUserMe } from '@/lib/config';

function DefaultRedirect() {
  const [target, setTarget] = useState<string | null>(null);
  useEffect(() => {
    Promise.all([getAppConfig(), getUserMe()]).then(([cfg, me]) => {
      const chatEnabled = cfg.chat?.enabled ?? true;
      if (me.can_compare) setTarget('/compare');
      else if (chatEnabled && me.can_chat) setTarget('/chat');
      else setTarget('/no-access');
    });
  }, []);
  if (!target) return null;
  return <Navigate to={target} replace />;
}

function NoAccessPage() {
  return (
    <AccessDenied
      title="No access"
      message="Your account isn't granted access to any QualiBOT feature. Contact your administrator to request access to the document comparison and/or the chat assistant."
    />
  );
}

function Layout() {
  const location = useLocation();
  const isChat = location.pathname.startsWith('/chat');

  return (
    <div
      className="h-screen flex flex-col overflow-hidden"
      style={{ background: 'var(--color-bg-primary)', color: 'var(--color-text-primary)', fontFamily: 'var(--font-body)' }}
    >
      <TopBar />
      <div className="flex-shrink-0 h-[var(--header-height)]" />
      <main className={`flex-1 ${isChat ? 'overflow-hidden flex flex-col' : 'overflow-auto'}`}>
        <Routes>
          <Route path="/compare"   element={<ComparePage />} />
          <Route path="/chat"      element={<ChatPage />} />
          <Route path="/chat/shared/:token" element={<SharedChatPage />} />
          <Route path="/no-access" element={<NoAccessPage />} />
          <Route path="*"          element={<DefaultRedirect />} />
        </Routes>
      </main>
    </div>
  );
}

export default function App() {
  return (
    <CustomThemeProvider>
      <BrowserRouter>
        <Layout />
      </BrowserRouter>
    </CustomThemeProvider>
  );
}
