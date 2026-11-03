import { Fragment, useEffect } from "react";
import { useIntegrationsHealth } from "../lib/integrationsHealth";
import { useSearchJobs } from "../lib/searchJobs";
import { TABS, type TabId } from "../nav";
import { RecentSearches } from "./RecentSearches";

export function Sidebar({
  activeTab,
  onSelect,
  collapsed,
  mobileOpen,
  onToggleCollapse,
  user,
  onSignOut,
}: {
  activeTab: TabId;
  onSelect: (id: TabId) => void;
  collapsed: boolean;
  mobileOpen: boolean;
  onToggleCollapse: () => void;
  user?: string;
  onSignOut?: () => void;
}) {
  const cls = ["sidebar", collapsed && "is-collapsed", mobileOpen && "is-open"]
    .filter(Boolean)
    .join(" ");

  // "Finished" indicator on the Matter Search item: shows when a background
  // search completes while you're elsewhere; being on Matter Search clears it
  // (markSeen re-runs on each poll while it's the active tab).
  const { unread, markSeen, searches } = useSearchJobs();
  useEffect(() => {
    if (activeTab === "matter-search") markSeen();
  }, [activeTab, searches, markSeen]);

  // Pulsing red dot on the Integrations item when a connector has failed 2+
  // consecutive health checks — a live status, shown even while the tab is open.
  const { alerting } = useIntegrationsHealth();

  return (
    <aside className={cls} aria-label="Modules">
      <div className="sidebar-top">
        <div className="brand">
          <span className="brand-logo wordmark">Aphrodite</span>
        </div>
        <button
          className="collapse-btn"
          onClick={onToggleCollapse}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          title={collapsed ? "Expand" : "Collapse"}
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d={collapsed ? "M9 6l6 6-6 6" : "M15 6l-6 6 6 6"} />
          </svg>
        </button>
      </div>

      <nav className="nav">
        {TABS.map((tab) => {
          const active = tab.id === activeTab;
          // The recent-searches (conversation) list nests under Matter Search,
          // only while it's the open tab and the rail isn't collapsed.
          const showRecent = tab.id === "matter-search" && active && !collapsed;
          return (
            <Fragment key={tab.id}>
              <button
                className={`nav-item${active ? " is-active" : ""}`}
                onClick={() => onSelect(tab.id)}
                title={collapsed ? tab.label : undefined}
                aria-current={active ? "page" : undefined}
              >
                <span className="nav-icon" aria-hidden="true">
                  <svg
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth={1.6}
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  >
                    {tab.icon}
                  </svg>
                </span>
                <span className="nav-label">{tab.label}</span>
                {tab.id === "matter-search" && !active && unread > 0 && (
                  <span
                    className="nav-dot"
                    aria-label={`${unread} search${unread === 1 ? "" : "es"} finished`}
                    title="A search finished"
                  />
                )}
                {tab.id === "integrations" && alerting && (
                  <span
                    className="nav-dot nav-dot-alert"
                    aria-label="An integration is down"
                    title="An integration is down"
                  />
                )}
                {!tab.ready && <span className="nav-soon">soon</span>}
              </button>
              {showRecent && (
                <div className="recent">
                  <RecentSearches />
                </div>
              )}
            </Fragment>
          );
        })}
      </nav>

      {user && (
        <div className="sidebar-foot">
          <span className="account-email" title={user}>
            {user}
          </span>
          <button className="link-btn" onClick={onSignOut}>
            Sign out
          </button>
          <div className="built-by">
            <span>Built by:</span>
            <a href="https://www.loop8.io" target="_blank" rel="noopener noreferrer" aria-label="Loop8">
              <img className="built-by-logo" src="/loop8-logo-white.png" alt="Loop8" />
            </a>
          </div>
        </div>
      )}
    </aside>
  );
}
