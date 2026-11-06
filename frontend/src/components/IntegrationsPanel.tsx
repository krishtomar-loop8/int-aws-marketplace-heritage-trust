import { Fragment, useState } from "react";
import { ConnectorCard } from "./ConnectorCard";
import { useIntegrationsHealth } from "../lib/integrationsHealth";
import type { IntegrationStatus, IntegrationTransition } from "../types";

// The Integrations tab — live health of every connected source system from the
// scheduled probe. Shows each connector's status, when it was last checked, and,
// when one is down, the error plus a plain-language suggested fix. A downtime log
// below records every up↔down change. Read-only for everyone except admins, who
// get a Connect/Reconnect control per connector (see ConnectorCard).

const LABELS: Record<string, string> = {
  clio: "Clio",
  estateably: "Estateably",
  graph: "Outlook / Microsoft 365",
  quickbooks: "QuickBooks",
  sharepoint: "SharePoint",
};

// The four connectors ConnectorCard knows how to connect/reconnect — "sharepoint"
// is a separately-tracked health row with no connect form of its own (it shares
// the "graph" credential).
const CONNECTABLE = new Set(["clio", "estateably", "graph", "quickbooks"]);

function label(id: string): string {
  return LABELS[id] ?? id;
}

function relativeTime(iso?: string): string {
  if (!iso) return "—";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return iso;
  const secs = Math.round((Date.now() - then) / 1000);
  if (secs < 60) return "just now";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins} min ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs} hr ago`;
  const days = Math.round(hrs / 24);
  if (days < 7) return `${days} day${days === 1 ? "" : "s"} ago`;
  return new Date(then).toLocaleDateString();
}

function absoluteTime(iso?: string): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  return Number.isNaN(t) ? iso : new Date(t).toLocaleString();
}

function StatusRow({
  row,
  isAdmin,
  expanded,
  onToggle,
}: {
  row: IntegrationStatus;
  isAdmin: boolean;
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <tr>
      <td>
        <div className="matter-name">{label(row.integration)}</div>
      </td>
      <td>
        <span className={`badge int-pill ${row.ok ? "is-ok" : "is-down"}`}>
          {row.ok ? "Operational" : "Down"}
        </span>
        {!row.ok && row.consecutive_failures > 1 && (
          <div className="matter-sub">
            {row.consecutive_failures} checks in a row
          </div>
        )}
      </td>
      <td>
        <div className="matter-name" title={absoluteTime(row.checked_at)}>
          {relativeTime(row.checked_at)}
        </div>
        <div className="matter-sub">{absoluteTime(row.checked_at)}</div>
      </td>
      <td>
        {row.ok ? (
          <span className="int-ok-note">No issues.</span>
        ) : (
          <>
            {row.detail && <div className="int-detail">{row.detail}</div>}
            {row.suggested_fix && (
              <div className="int-fix">
                <strong>Suggested fix:</strong> {row.suggested_fix}
              </div>
            )}
          </>
        )}
      </td>
      {isAdmin && (
        <td>
          {CONNECTABLE.has(row.integration) && (
            <button className="link-btn ghost" onClick={onToggle}>
              {expanded ? "Close" : row.checked_at ? "Reconnect" : "Connect"}
            </button>
          )}
        </td>
      )}
    </tr>
  );
}

function LogRow({ entry }: { entry: IntegrationTransition }) {
  return (
    <tr>
      <td>
        <div className="matter-name" title={absoluteTime(entry.at)}>
          {relativeTime(entry.at)}
        </div>
        <div className="matter-sub">{absoluteTime(entry.at)}</div>
      </td>
      <td>{label(entry.integration)}</td>
      <td>
        <span className={`int-change ${entry.ok ? "is-ok" : "is-down"}`}>
          {entry.ok ? "Recovered" : "Went down"}
        </span>
      </td>
      <td>{!entry.ok && entry.detail ? <span className="int-detail">{entry.detail}</span> : "—"}</td>
    </tr>
  );
}

export function IntegrationsPanel({
  apiBase,
  token,
  isAdmin = false,
}: {
  apiBase: string;
  token?: string;
  isAdmin?: boolean;
}) {
  const { health, loading, refresh } = useIntegrationsHealth();
  const { integrations, log, alerting } = health;
  const down = integrations.filter((i) => !i.ok);
  const [expanded, setExpanded] = useState<string | null>(null);

  function toggle(integration: string) {
    setExpanded((cur) => (cur === integration ? null : integration));
  }

  return (
    <div className="reports">
      <div className="reports-head">
        <h1 className="reports-title">Integrations</h1>
        <p className="reports-lede">
          Live health of every connected system — Clio, Estateably, Outlook, and QuickBooks. An
          automated check runs each hour so a connector that stops responding (for example, an
          expired access token) is caught here rather than failing quietly inside a search.
        </p>
      </div>

      <div className="reports-controls">
        <button className="link-btn ghost" onClick={() => refresh()}>
          Refresh
        </button>
      </div>

      {alerting && (
        <p className="banner banner-error">
          {down.length === 1
            ? `${label(down[0].integration)} is down. See the suggested fix below.`
            : `${down.length} integrations are down. See the suggested fixes below.`}
        </p>
      )}
      {!alerting && integrations.length > 0 && (
        <p className="banner banner-ok">All connected systems are operational.</p>
      )}

      {loading && integrations.length === 0 && (
        <div className="status-note">
          <span className="spinner" aria-hidden="true" />
          Loading integration status…
        </div>
      )}

      {!loading && integrations.length === 0 && (
        <>
          <p className="banner banner-warn">
            No health checks recorded yet. The hourly connector check populates this once it is
            enabled — status will appear here after the first run.
          </p>
          {isAdmin && (
            <div className="reports">
              <p className="reports-lede">
                You don't need to wait for the first check to connect a source — use any of the
                forms below.
              </p>
              {Array.from(CONNECTABLE).map((integration) => (
                <div key={integration} className="connector-card-block">
                  <h3>{label(integration)}</h3>
                  <ConnectorCard integration={integration} apiBase={apiBase} token={token} />
                </div>
              ))}
            </div>
          )}
        </>
      )}

      {integrations.length > 0 && (
        <div className="table-scroll">
          <table className="matter-table">
            <thead>
              <tr>
                <th>System</th>
                <th>Status</th>
                <th>Last checked</th>
                <th>Details</th>
                {isAdmin && <th>Setup</th>}
              </tr>
            </thead>
            <tbody>
              {integrations.map((row) => (
                <Fragment key={row.integration}>
                  <StatusRow
                    row={row}
                    isAdmin={isAdmin}
                    expanded={expanded === row.integration}
                    onToggle={() => toggle(row.integration)}
                  />
                  {isAdmin && expanded === row.integration && CONNECTABLE.has(row.integration) && (
                    <tr>
                      <td colSpan={5}>
                        <ConnectorCard integration={row.integration} apiBase={apiBase} token={token} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {log.length > 0 && (
        <>
          <div className="reports-head" style={{ marginTop: "1.8rem" }}>
            <h2 className="reports-title" style={{ fontSize: "1.15rem" }}>
              Recent status changes
            </h2>
            <p className="reports-lede">
              Every time a connector went down or recovered, newest first.
            </p>
          </div>
          <div className="table-scroll">
            <table className="matter-table">
              <thead>
                <tr>
                  <th>When</th>
                  <th>System</th>
                  <th>Change</th>
                  <th>Details</th>
                </tr>
              </thead>
              <tbody>
                {log.map((entry, i) => (
                  <LogRow key={`${entry.integration}-${entry.at}-${i}`} entry={entry} />
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
