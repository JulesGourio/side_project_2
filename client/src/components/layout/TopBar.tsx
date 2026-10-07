import { Link, useLocation } from 'react-router-dom';
import { useEffect, useState } from 'react';
import { getAppConfig, type AppBranding } from '@/lib/config';


const ALL_TABS = [
  { id: 'compare',   label: 'Compare',   href: '/compare' },
  { id: 'chat',      label: 'Chat KA',   href: '/chat' },
  { id: 'chat-vsi',  label: 'Chat VSI',  href: '/chat-vsi' },
] as const;

export function TopBar() {
  const location = useLocation();
  const [branding, setBranding] = useState<AppBranding>({
    name: 'Document Compare',
    logo: '/logos/LOGO_LATECOERE.png',
  });

  useEffect(() => {
    getAppConfig().then((cfg) => setBranding(cfg.branding));
  }, []);

  // Both tabs are always shown; a feature the user isn't entitled to renders an
  // "access not granted" panel (and the backend returns 403) rather than hiding.
  const tabs = ALL_TABS;

  const activeTab = location.pathname.startsWith('/chat-vsi') ? 'chat-vsi'
    : location.pathname.startsWith('/chat') ? 'chat' : 'compare';

  return (
    <header
      className="fixed top-0 left-0 right-0 z-30 h-[var(--header-height)]"
      style={{
        background: 'rgba(12, 28, 62, 0.92)',
        backdropFilter: 'blur(12px) saturate(1.4)',
        WebkitBackdropFilter: 'blur(12px) saturate(1.4)',
        borderBottom: '1px solid rgba(255,255,255,0.07)',
        boxShadow: '0 1px 24px rgba(0,0,0,0.25)',
      }}
    >
      <div className="flex items-center justify-between h-full px-5 lg:px-8 max-w-7xl mx-auto">

        {/* Logo + App title */}
        <Link to="/compare" className="flex items-center gap-3 flex-shrink-0">
          <img
            src={branding.logo}
            alt=""
            className="h-7 w-auto object-contain brightness-0 invert opacity-90"
          />
          <span
            className="text-lg font-bold tracking-tight select-none"
            style={{ color: 'rgba(255,255,255,0.95)', fontFamily: 'var(--font-heading)', letterSpacing: '-0.01em' }}
          >
            QualiBOT
          </span>
        </Link>

        {/* Navigation */}
        <nav className="flex items-center gap-1">
          {tabs.map((tab) => {
            const isActive = activeTab === tab.id;
            return (
              <Link
                key={tab.id}
                to={tab.href}
                className="relative px-4 py-1.5 text-sm font-medium rounded-lg transition-all duration-200"
                style={{
                  color: isActive ? '#ffffff' : 'rgba(255,255,255,0.55)',
                  background: isActive ? 'rgba(255,255,255,0.1)' : 'transparent',
                }}
                onMouseEnter={e => {
                  if (!isActive) e.currentTarget.style.color = 'rgba(255,255,255,0.85)';
                  if (!isActive) e.currentTarget.style.background = 'rgba(255,255,255,0.06)';
                }}
                onMouseLeave={e => {
                  if (!isActive) e.currentTarget.style.color = 'rgba(255,255,255,0.55)';
                  if (!isActive) e.currentTarget.style.background = 'transparent';
                }}
              >
                {tab.label}
                {isActive && (
                  <span
                    className="absolute bottom-0 left-3 right-3 h-px rounded-full"
                    style={{
                      background: 'linear-gradient(90deg, transparent, #4DA3E8, transparent)',
                      animation: 'slideIn 0.25s ease-out',
                    }}
                  />
                )}
              </Link>
            );
          })}
        </nav>
      </div>
    </header>
  );
}
