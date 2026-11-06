import { useState } from "react";
import { ActivityLogPanel } from "./components/ActivityLogPanel";
import { AmlExportPanel } from "./components/AmlExportPanel";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { IntegrationsPanel } from "./components/IntegrationsPanel";
import { MatterSearchChat } from "./components/MatterSearchChat";
import { Placeholder } from "./components/Placeholder";
import { ReportsPanel } from "./components/ReportsPanel";
import { ScreenerPanel } from "./components/ScreenerPanel";
import { Sidebar } from "./components/Sidebar";
import { IntegrationsHealthProvider } from "./lib/integrationsHealth";
import { SearchJobsProvider } from "./lib/searchJobs";
import { DEFAULT_TAB, TABS, type TabId } from "./nav";

export function App({
  apiBase,
  token,
  mock,
  user,
  onSignOut,
  isAdmin,
}: {
  apiBase: string;
  token?: string;
  mock: boolean;
  user?: string;
  onSignOut?: () => void;
  isAdmin?: boolean;
}) {
  const [activeTab, setActiveTab] = useState<TabId>(DEFAULT_TAB);
  const [collapsed, setCollapsed] = useState(false); // desktop rail
  const [mobileOpen, setMobileOpen] = useState(false); // mobile drawer

  const active = TABS.find((t) => t.id === activeTab) ?? TABS[0];

  function select(id: TabId) {
    setActiveTab(id);
    setMobileOpen(false); // close the drawer after choosing on mobile
  }

  return (
    <SearchJobsProvider apiBase={apiBase} token={token} mock={mock}>
      <IntegrationsHealthProvider apiBase={apiBase} token={token} mock={mock}>
        <div className={`shell${collapsed ? " is-collapsed" : ""}`}>
          <header className="topbar">
            <button
              className="hamburger"
              onClick={() => setMobileOpen((v) => !v)}
              aria-label="Toggle menu"
              aria-expanded={mobileOpen}
            >
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d="M4 6h16M4 12h16M4 18h16" />
              </svg>
            </button>
            <span className="topbar-brand">
              <span className="topbar-logo wordmark">Aphrodite</span>
            </span>
          </header>

          <Sidebar
            activeTab={activeTab}
            onSelect={select}
            collapsed={collapsed}
            mobileOpen={mobileOpen}
            onToggleCollapse={() => setCollapsed((v) => !v)}
            user={user}
            onSignOut={onSignOut}
          />

          {mobileOpen && (
            <div className="scrim" onClick={() => setMobileOpen(false)} aria-hidden="true" />
          )}

          <main className="content">
            {/* Per-tab render safety net: a crash in one panel shows a fallback here
                instead of blanking the whole app; the key resets it on tab switch. */}
            <ErrorBoundary key={activeTab}>
              {activeTab === "matter-search" ? (
                <MatterSearchChat apiBase={apiBase} token={token} mock={mock} />
              ) : activeTab === "quarterly-reports" ? (
                <ReportsPanel apiBase={apiBase} token={token} mock={mock} />
              ) : activeTab === "aml-export" ? (
                <AmlExportPanel apiBase={apiBase} token={token} mock={mock} />
              ) : activeTab === "sanction-screener" ? (
                <ScreenerPanel apiBase={apiBase} token={token} mock={mock} />
              ) : activeTab === "activity-log" ? (
                <ActivityLogPanel apiBase={apiBase} token={token} mock={mock} />
              ) : activeTab === "integrations" ? (
                <IntegrationsPanel apiBase={apiBase} token={token} isAdmin={isAdmin} />
              ) : (
                <Placeholder tab={active} />
              )}
            </ErrorBoundary>
          </main>
        </div>
      </IntegrationsHealthProvider>
    </SearchJobsProvider>
  );
}
