import { useEffect, useState } from "react";
import { type Capabilities, fetchCapabilities, fetchSetupStatus } from "./api";
import { ActivityLogPanel } from "./components/ActivityLogPanel";
import { AmlExportPanel } from "./components/AmlExportPanel";
import { DeploymentConfigPanel } from "./components/DeploymentConfigPanel";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { IntegrationsPanel } from "./components/IntegrationsPanel";
import { MatterSearchChat } from "./components/MatterSearchChat";
import { Placeholder } from "./components/Placeholder";
import { ReportsPanel } from "./components/ReportsPanel";
import { ScreenerPanel } from "./components/ScreenerPanel";
import { Sidebar } from "./components/Sidebar";
import { SetupWizard } from "./components/SetupWizard";
import { IntegrationsHealthProvider } from "./lib/integrationsHealth";
import { SearchJobsProvider } from "./lib/searchJobs";
import { DEFAULT_TAB, TABS, type TabId } from "./nav";

// Shown in place of the wizard for a non-admin while setup is undismissed —
// a non-admin can't complete any wizard step, so there's nothing useful to
// show them but a wait message and a way out.
function SetupPending({ onSignOut }: { onSignOut?: () => void }) {
  return (
    <div
      style={{
        minHeight: "100vh",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: "1rem",
        textAlign: "center",
        padding: "0 1.5rem",
      }}
    >
      <span className="wordmark" style={{ color: "var(--forest)", fontSize: "1.15rem" }}>
        Aphrodite
      </span>
      <p>This deployment hasn't finished setup yet. Ask an admin to complete it.</p>
      {onSignOut && (
        <button className="link-btn" style={{ background: "var(--forest)", color: "#f4f1e8", borderColor: "var(--forest)" }} onClick={onSignOut}>
          Sign out
        </button>
      )}
    </div>
  );
}

// Default to everything visible until /capabilities resolves (or in mock mode,
// where there's no backend to ask) — a transient fetch error must never hide a
// tab that should be visible.
const ALL_CAPABILITIES: Capabilities = {
  modules: { quarterly_reports: true, aml_export: true, sanction_screener: true },
  connectors: { clio: true, estateably: true, quickbooks: true, graph: true },
};

const MODULE_CAPABILITY_KEY: Partial<Record<TabId, keyof Capabilities["modules"]>> = {
  "quarterly-reports": "quarterly_reports",
  "aml-export": "aml_export",
  "sanction-screener": "sanction_screener",
};

// Read once at module scope — the QuickBooks OAuth callback redirects the
// browser here with ?setup=quickbooks-connected|quickbooks-error, since
// that leg of the flow has no in-page fetch response to show an error on
// (the browser leaves the app entirely for the Intuit consent screen).
function readSetupResult(): string | null {
  if (typeof window === "undefined") return null;
  return new URLSearchParams(window.location.search).get("setup");
}

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
  const [setupResult] = useState<string | null>(readSetupResult);
  const [activeTab, setActiveTab] = useState<TabId>(() =>
    readSetupResult() ? "integrations" : DEFAULT_TAB,
  );
  const [collapsed, setCollapsed] = useState(false); // desktop rail
  const [mobileOpen, setMobileOpen] = useState(false); // mobile drawer
  const [capabilities, setCapabilities] = useState<Capabilities>(ALL_CAPABILITIES);
  // Fail closed (false) while unresolved — hiding setup from an admin by
  // accident is worse than briefly showing it an extra time. Mock mode has no
  // backend to ask, so it stays fixed at dismissed, same as ALL_CAPABILITIES
  // above is the mock-mode default for capabilities, not a fetched value.
  const [dismissed, setDismissed] = useState(mock);

  const visibleTabs = TABS.filter((t) => {
    const key = MODULE_CAPABILITY_KEY[t.id];
    return !key || capabilities.modules[key];
  });
  const active = visibleTabs.find((t) => t.id === activeTab) ?? visibleTabs[0];

  // Clear the query param once read so a page refresh doesn't re-show it.
  useEffect(() => {
    if (setupResult) {
      window.history.replaceState({}, document.title, window.location.pathname);
    }
  }, [setupResult]);

  useEffect(() => {
    if (mock) return; // no backend to ask in mock mode — stay at all-enabled
    let cancelled = false;
    fetchCapabilities(apiBase, token).then((caps) => {
      if (!cancelled) setCapabilities(caps);
    });
    return () => {
      cancelled = true;
    };
  }, [apiBase, token, mock]);

  useEffect(() => {
    if (mock) return; // no backend to ask in mock mode — stay dismissed
    let cancelled = false;
    fetchSetupStatus(apiBase, token).then((status) => {
      if (!cancelled) setDismissed(status.dismissed);
    });
    return () => {
      cancelled = true;
    };
  }, [apiBase, token, mock]);

  function select(id: TabId) {
    setActiveTab(id);
    setMobileOpen(false); // close the drawer after choosing on mobile
  }

  return (
    <SearchJobsProvider apiBase={apiBase} token={token} mock={mock}>
      <IntegrationsHealthProvider apiBase={apiBase} token={token} mock={mock}>
        {!mock && !dismissed ? (
          !isAdmin ? (
            <SetupPending onSignOut={onSignOut} />
          ) : (
            <SetupWizard
              apiBase={apiBase}
              token={token}
              capabilities={capabilities}
              setupResult={setupResult}
              onDismissed={() => setDismissed(true)}
            />
          )
        ) : (
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
              tabs={visibleTabs}
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
                  <IntegrationsPanel
                    apiBase={apiBase}
                    token={token}
                    isAdmin={isAdmin}
                    setupResult={setupResult}
                    capabilities={capabilities}
                  />
                ) : activeTab === "deployment-config" ? (
                  <DeploymentConfigPanel apiBase={apiBase} token={token} isAdmin={isAdmin} mock={mock} />
                ) : (
                  <Placeholder tab={active} />
                )}
              </ErrorBoundary>
            </main>
          </div>
        )}
      </IntegrationsHealthProvider>
    </SearchJobsProvider>
  );
}
