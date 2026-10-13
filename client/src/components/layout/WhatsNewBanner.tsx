import { Newspaper, X, ExternalLink } from 'lucide-react';

const TEAMS_CHANNEL_URL =
  'https://teams.microsoft.com/l/channel/19%3Acd4f046237804a1c8e9c830bd8267aac%40thread.tacv2/Qualibot?groupId=768d4e4c-de5e-48cf-80d1-54befd55fb09&tenantId=bbd06402-4bce-4ba0-81fd-babe864e4af1';

export type TabId = 'compare' | 'chat';

// Placeholder copy — meant to be edited by hand as real news comes in. One
// news list per tool page, newest entry first; add more objects to `news` as
// updates roll out — the list scrolls independently of the header below.
const README_CONTENT: Record<TabId, { title: string; news: { date: string; features: string[] }[] }> = {
  compare: {
    title: "Compare — What's New",
    news: [
      { 
        date: '2026-07-09', 
        features: [
          "Welcome to the Compare tab! This space will list the latest news, improvements and fixes for document comparison."
        ] 
      },
    ],
  },
  chat: {
    title: "Chatbot — What's New",
    news: [
      {
        date: '2026-08-17',
        features: [
          "Share a conversation with a read-only link — click \"Share\" to copy it; anyone in Qualibot with the link can view it live, and can duplicate it into their own history to keep asking questions.",
        ]
      },
      {
        date: '2026-07-09',
        features: [
          "Displaying language options for available documents.",
          "Improving query management in all languages.",
          "Improvement in handling PowerPoint files and images."
        ] 
      },
      { 
        date: '2026-07-09', 
        features: [
          "Welcome to the Chatbot tab! Find the latest updates to the QualiBOT assistant here."
        ] 
      },
    ],
  },
};

// Small button meant to sit inline in a tool's own toolbar (e.g. next to
// "Knowledge Assistant" on the Chat page). It only toggles visibility —
// the caller owns the open/closed state and renders <WhatsNewModal> itself.
export function WhatsNewButton({ tabId, open, onClick }: { tabId: TabId; open: boolean; onClick: () => void }) {
  const content = README_CONTENT[tabId];
  return (
    <button
      onClick={onClick}
      className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs font-medium border transition-all cursor-pointer"
      style={{
        borderColor: open ? 'var(--color-accent-primary)' : 'var(--color-border)',
        color: open ? 'var(--color-accent-primary)' : 'var(--color-text-muted)',
        background: 'transparent',
      }}
      onMouseEnter={e => { if (!open) e.currentTarget.style.color = 'var(--color-text-primary)'; }}
      onMouseLeave={e => { if (!open) e.currentTarget.style.color = 'var(--color-text-muted)'; }}
      title={content.title}
    >
      <Newspaper className="h-3.5 w-3.5" />
      What's new
    </button>
  );
}

// Popup that opens above the page. Two fixed sections (title+close, then the
// Teams link) stay put; only the news list underneath scrolls — meant to
// stay usable once there are many entries.
export function WhatsNewModal({ tabId, onClose }: { tabId: TabId; onClose: () => void }) {
  const content = README_CONTENT[tabId];

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="relative flex flex-col rounded-2xl shadow-2xl overflow-hidden"
        style={{ width: '50vw', height: '70vh', background: 'var(--color-background)' }}
        onClick={e => e.stopPropagation()}
      >
        {/* Static: title + close */}
        <div
          className="flex items-center justify-between gap-3 px-6 py-4 border-b flex-shrink-0"
          style={{ borderColor: 'var(--color-border)' }}
        >
          <div className="flex items-center gap-3 min-w-0">
            <div
              className="flex-shrink-0 w-11 h-11 rounded-xl flex items-center justify-center"
              style={{ background: 'var(--color-accent-primary)', color: '#fff' }}
            >
              <Newspaper className="h-5.5 w-5.5" />
            </div>
            <span className="text-lg font-semibold truncate" style={{ color: 'var(--color-text-heading)' }}>
              {content.title}
            </span>
          </div>
          <button
            onClick={onClose}
            className="w-9 h-9 rounded-lg flex items-center justify-center transition-all cursor-pointer flex-shrink-0"
            style={{ color: 'var(--color-text-muted)' }}
            onMouseEnter={e => { e.currentTarget.style.color = 'var(--color-error)'; }}
            onMouseLeave={e => { e.currentTarget.style.color = 'var(--color-text-muted)'; }}
            title="Close"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* Static: Teams link */}
        <div className="px-6 py-4 border-b flex-shrink-0" style={{ borderColor: 'var(--color-border)' }}>
          <a
            href={TEAMS_CHANNEL_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-2 text-base font-medium hover:underline"
            style={{ color: 'var(--color-accent-primary)' }}
          >
            Open the Qualibot Teams channel
            <ExternalLink className="h-4 w-4" />
          </a>
        </div>

        {/* Scrollable: news list */}
        <div className="flex-1 overflow-y-auto px-6 py-5 space-y-6">
          {content.news.map((item, i) => (
            <div 
              key={i} 
              className={i > 0 ? 'pt-6 border-t' : ''} 
              style={{ borderColor: 'var(--color-border)' }}
            >
              {/* Date Badge */}
              <div className="mb-3">
                <span 
                  className="inline-block px-2.5 py-0.5 rounded-md text-xs font-semibold select-none" 
                  style={{ 
                    background: 'var(--color-background-muted, rgba(0,0,0,0.05))', 
                    color: 'var(--color-text-muted)' 
                  }}
                >
                  {item.date}
                </span>
              </div>
              
              {/* Features List */}
              <ul className="space-y-2.5">
                {item.features.map((feature, index) => (
                  <li 
                    key={index} 
                    className="flex items-start gap-2.5 text-base leading-relaxed" 
                    style={{ color: 'var(--color-text-primary)' }}
                  >
                    {/* Custom bullet dot matching accent color */}
                    <span 
                      className="mt-1.5 h-1.5 w-1.5 rounded-full flex-shrink-0" 
                      style={{ background: 'var(--color-accent-primary)' }} 
                    />
                    <span>{feature}</span>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}