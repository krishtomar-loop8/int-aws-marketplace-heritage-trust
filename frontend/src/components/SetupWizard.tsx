import { useEffect, useState } from "react";
import { type Capabilities, dismissSetup, fetchConfig } from "../api";
import { useIntegrationsHealth } from "../lib/integrationsHealth";
import { ConnectorCard } from "./ConnectorCard";
import { DeploymentConfigPanel } from "./DeploymentConfigPanel";

// First-run setup wizard — a full-screen, step-by-step walk through connecting
// each connector this deployment has enabled, then business config (design
// spec docs/superpowers/specs/2026-11-12-first-run-setup-wizard-design.md
// §5-§6a). Rendered only for an admin, in place of the normal app shell, until
// the persisted "setup dismissed" flag is set. Step content reuses the
// existing ConnectorCard / DeploymentConfigPanel forms unchanged.

function errorMessage(e: unknown): string {
  return e instanceof Error ? e.message : "Something went wrong.";
}

// Fixed step order (spec §5) — a connector disabled for this deployment gets no step.
const CONNECTOR_ORDER = ["clio", "estateably", "quickbooks", "graph"] as const;
type ConnectorName = (typeof CONNECTOR_ORDER)[number];

const LABELS: Record<ConnectorName, string> = {
  clio: "Clio",
  estateably: "Estateably",
  quickbooks: "QuickBooks",
  graph: "Outlook / Microsoft 365",
};

// The base .link-btn is tuned for the dark sidebar (light text, no fill) and is
// unreadable on this light page — same solid forest treatment styles.css gives
// .matter-table / .report-list primary buttons.
const PRIMARY_BTN = { background: "var(--forest)", color: "#f4f1e8", borderColor: "var(--forest)" };

const BUSINESS_CONFIG = "business-config";
type StepId = ConnectorName | typeof BUSINESS_CONFIG;

export function SetupWizard({
  apiBase,
  token,
  capabilities,
  setupResult,
  onDismissed,
}: {
  apiBase: string;
  token?: string;
  capabilities: Capabilities;
  setupResult: string | null;
  onDismissed: () => void;
}) {
  const { health } = useIntegrationsHealth();

  // Always ends with exactly one business-config step, so the list is never
  // empty even when every connector is disabled.
  const steps: StepId[] = [
    ...CONNECTOR_ORDER.filter((name) => capabilities.connectors[name]),
    BUSINESS_CONFIG,
  ];

  // The QuickBooks OAuth cold reload resumes at the QuickBooks step (spec §6a);
  // every other load starts at the first step.
  const isQuickBooksResult =
    setupResult === "quickbooks-connected" || setupResult === "quickbooks-error";
  // Tracked by step id, not index: App's capabilities start all-enabled and
  // resolve asynchronously, which can remove steps after first render — an
  // index would then silently point at a different step.
  const [currentId, setCurrentId] = useState<StepId>(() =>
    isQuickBooksResult ? "quickbooks" : steps[0],
  );
  const [skipped, setSkipped] = useState<Set<StepId>>(() => new Set());
  const [configDone, setConfigDone] = useState(false);
  const [dismissing, setDismissing] = useState(false);
  const [dismissError, setDismissError] = useState<string | null>(null);

  // Business-config "already done": at least one field configured (spec §6).
  // Checked once on mount; a load failure just leaves the step not-done.
  useEffect(() => {
    let cancelled = false;
    fetchConfig(apiBase, token)
      .then((res) => {
        if (!cancelled) {
          setConfigDone(Object.values(res.fields).some((f) => f.source === "configured"));
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [apiBase, token]);

  // A step that's no longer in the list (its connector turned out to be
  // disabled once capabilities resolved) falls back to the first step.
  const current = steps.includes(currentId) ? currentId : steps[0];
  const position = steps.indexOf(current);

  // Connector "already done": a health row exists, regardless of row.ok (spec §6).
  function isDone(step: StepId): boolean {
    if (step === BUSINESS_CONFIG) return configDone;
    return health.integrations.some((row) => row.integration === step);
  }

  function stepLabel(step: StepId): string {
    return step === BUSINESS_CONFIG ? "Business Config" : LABELS[step];
  }

  function advance() {
    setCurrentId(steps[Math.min(position + 1, steps.length - 1)]);
  }

  function skip() {
    setSkipped((s) => new Set(s).add(current));
    advance();
  }

  async function finish() {
    setDismissing(true);
    setDismissError(null);
    try {
      await dismissSetup(apiBase, token);
      onDismissed();
    } catch (e) {
      setDismissError(errorMessage(e));
      setDismissing(false);
    }
  }

  // Show the QuickBooks callback result on the QuickBooks step — or, if this
  // deployment has no QuickBooks step, on whichever step is showing.
  const showQuickBooksBanner =
    isQuickBooksResult && (current === "quickbooks" || !steps.includes("quickbooks"));

  const currentDone = isDone(current);

  return (
    <div style={{ minHeight: "100vh", display: "flex", flexDirection: "column" }}>
      <div className="reports">
        <div className="reports-head">
          <span className="wordmark" style={{ color: "var(--forest)", fontSize: "1.15rem" }}>
            Aphrodite
          </span>
          <h1 className="reports-title" style={{ marginTop: "0.8rem" }}>
            Set up your deployment
          </h1>
          <p className="reports-lede">
            Connect each source system this deployment uses, then set your business
            configuration. Anything you skip can be finished later from the Integrations and
            Deployment Config tabs.
          </p>
        </div>

        <ol
          aria-label="Setup progress"
          style={{ listStyle: "none", padding: 0, margin: "0 0 1.4rem", display: "flex", flexWrap: "wrap", gap: "0.5rem 1.2rem" }}
        >
          {steps.map((step, i) => {
            const done = isDone(step);
            const active = step === current;
            return (
              <li
                key={step}
                aria-current={active ? "step" : undefined}
                style={{ display: "flex", alignItems: "center", gap: "0.45rem", fontWeight: active ? 600 : 400, color: active ? "var(--forest-2)" : "#45514d" }}
              >
                <span>
                  {i + 1}. {stepLabel(step)}
                </span>
                {done ? (
                  <span className="badge badge-review">Done</span>
                ) : skipped.has(step) ? (
                  <span className="badge">Skipped</span>
                ) : null}
              </li>
            );
          })}
        </ol>

        {showQuickBooksBanner && setupResult === "quickbooks-connected" && (
          <p className="banner banner-ok">QuickBooks connected — validated.</p>
        )}
        {showQuickBooksBanner && setupResult === "quickbooks-error" && (
          <p className="banner banner-error">
            The QuickBooks connection didn't complete. Any previously-working connection is
            unaffected — check the details below and try again.
          </p>
        )}

        {current === BUSINESS_CONFIG ? (
          <DeploymentConfigPanel apiBase={apiBase} token={token} isAdmin mock={false} />
        ) : (
          <div className="connector-card-block" style={{ marginTop: "1rem" }}>
            <h3>
              Step {position + 1} of {steps.length}: Connect {LABELS[current]}
            </h3>
            {currentDone && (
              <p className="schedule-status" style={{ marginBottom: "0.6rem" }}>
                Already connected — continue, or reconnect below.
              </p>
            )}
            <ConnectorCard integration={current} apiBase={apiBase} token={token} />
          </div>
        )}

        {dismissError && <p className="banner banner-error">{dismissError}</p>}

        <div className="reports-controls">
          {current === BUSINESS_CONFIG ? (
            // Last step: skip and finish are the same action here, so one button.
            <button className="link-btn" style={PRIMARY_BTN} onClick={finish} disabled={dismissing}>
              {dismissing ? <span className="spinner" aria-hidden="true" /> : "Finish setup"}
            </button>
          ) : (
            <>
              <button className="link-btn ghost" onClick={skip}>
                Skip for now
              </button>
              {currentDone && (
                <button className="link-btn" style={PRIMARY_BTN} onClick={advance}>
                  Continue
                </button>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
