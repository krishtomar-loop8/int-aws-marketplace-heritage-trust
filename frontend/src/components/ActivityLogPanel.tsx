import { useCallback, useEffect, useMemo, useState } from "react";
import { listActivity } from "../api";
import { MOCK_ACTIVITY } from "../mock";
import type { ActivityEntry } from "../types";

// The Activity Logs tab — the chain-of-custody audit trail. A read-only table of
// every staff action (who did what, when, and which source systems were read),
// newest first, with client-side pagination. Records are append-only; nothing
// here can be edited or deleted.

const PAGE_SIZES = [25, 50, 75, 100] as const;
const DEFAULT_PAGE_SIZE = 25;
const SERVER_WINDOW = 500; // matches the API's default /activity limit

// Human labels + tone for each machine action.
const ACTION_META: Record<string, { label: string; tone: string }> = {
  "matter.search": { label: "Matter search", tone: "search" },
  "report.generate": { label: "Report draft", tone: "report" },
  "aml.run": { label: "AML export", tone: "aml" },
  "aml.schedule": { label: "AML schedule", tone: "aml" },
  "screener.screen": { label: "Sanction screen", tone: "screen" },
  "screener.certificate": { label: "Certificate", tone: "cert" },
  "screener.delete": { label: "Certificate removed", tone: "danger" },
  download: { label: "Download", tone: "download" },
};

// Friendly names for the connector ids stored on search rows.
const SOURCE_LABELS: Record<string, string> = {
  clio: "Clio",
  estateably: "Estateably",
  graph: "Outlook",
  quickbooks: "QuickBooks",
  sharepoint: "SharePoint",
};

function actionMeta(action: string) {
  return ACTION_META[action] ?? { label: action, tone: "default" };
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

// Page numbers to render, with gaps (…) collapsed for long logs.
function pageWindow(current: number, total: number): (number | "gap")[] {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
  const out: (number | "gap")[] = [1];
  const lo = Math.max(2, current - 1);
  const hi = Math.min(total - 1, current + 1);
  if (lo > 2) out.push("gap");
  for (let p = lo; p <= hi; p++) out.push(p);
  if (hi < total - 1) out.push("gap");
  out.push(total);
  return out;
}

export function ActivityLogPanel({
  apiBase,
  token,
  mock,
}: {
  apiBase: string;
  token?: string;
  mock: boolean;
}) {
  const [entries, setEntries] = useState<ActivityEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState<number>(DEFAULT_PAGE_SIZE);

  const load = useCallback(async () => {
    const data = mock ? MOCK_ACTIVITY : await listActivity(apiBase, token);
    setEntries(data);
    return data;
  }, [apiBase, token, mock]);

  useEffect(() => {
    let alive = true;
    setError(null);
    setEntries(null);
    (async () => {
      try {
        await load();
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : "Failed to load activity");
      }
    })();
    return () => {
      alive = false;
    };
  }, [load]);

  const filtered = useMemo(() => {
    if (!entries) return [];
    const q = filter.trim().toLowerCase();
    if (!q) return entries;
    return entries.filter((e) =>
      [e.actor_email, e.summary, actionMeta(e.action).label, e.target, (e.sources || []).join(" ")]
        .filter(Boolean)
        .some((v) => (v as string).toLowerCase().includes(q)),
    );
  }, [entries, filter]);

  // Reset to the first page whenever the result set or page size changes.
  useEffect(() => {
    setPage(1);
  }, [filter, pageSize]);

  const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
  const current = Math.min(page, pageCount);
  const start = (current - 1) * pageSize;
  const rows = filtered.slice(start, start + pageSize);
  const atWindow = (entries?.length ?? 0) >= SERVER_WINDOW;

  return (
    <div className="reports">
      <div className="reports-head">
        <h1 className="reports-title">Activity Logs</h1>
        <p className="reports-lede">
          A complete, append-only chain-of-custody record: who queried what, when, and which
          source systems were read. Every staff action across the portal is logged here for audit.
          Records cannot be edited or removed.
        </p>
      </div>

      <div className="reports-controls">
        <label className="field field-grow">
          <span>Search</span>
          <input
            type="search"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Filter by user, activity, matter, or source"
          />
        </label>
        <label className="field">
          <span>Rows per page</span>
          <select value={pageSize} onChange={(e) => setPageSize(Number(e.target.value))}>
            {PAGE_SIZES.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
      </div>

      {error && <p className="banner banner-error">{error}</p>}

      {!entries && !error && (
        <div className="status-note">
          <span className="spinner" aria-hidden="true" />
          Loading activity…
        </div>
      )}

      {entries && entries.length === 0 && (
        <p className="banner banner-warn">
          No activity recorded yet. The audit trail begins from when this feature went live —
          actions taken from now on will appear here.
        </p>
      )}

      {entries && entries.length > 0 && filtered.length === 0 && (
        <p className="banner banner-warn">No activity matches “{filter}”.</p>
      )}

      {filtered.length > 0 && (
        <>
          <div className="table-scroll">
            <table className="matter-table activity-table">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Who</th>
                  <th>Activity</th>
                  <th>Details</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((e) => {
                  const meta = actionMeta(e.action);
                  return (
                    <tr key={e.id ?? `${e.timestamp}-${e.summary}`}>
                      <td>
                        <div className="matter-name" title={absoluteTime(e.timestamp)}>
                          {relativeTime(e.timestamp)}
                        </div>
                        <div className="matter-sub">{absoluteTime(e.timestamp)}</div>
                      </td>
                      {/* Rows recorded before the SPA sent the ID token carry
                          actor_email "unknown" — fall back to the Cognito sub so
                          Who still identifies the actor on historical entries. */}
                      <td>
                        {e.actor_email && e.actor_email !== "unknown"
                          ? e.actor_email
                          : e.actor_sub && e.actor_sub !== "-"
                            ? `${e.actor_sub.slice(0, 8)}…`
                            : "—"}
                      </td>
                      <td>
                        <span className={`badge action-badge action-${meta.tone}`}>
                          {meta.label}
                        </span>
                        <div className="activity-summary">{e.summary}</div>
                      </td>
                      <td>
                        {e.target && <div className="matter-sub activity-target">{e.target}</div>}
                        {e.sources && e.sources.length > 0 && (
                          <div className="source-chips">
                            {e.sources.map((s) => (
                              <span key={s} className="source-chip">
                                {SOURCE_LABELS[s] ?? s}
                              </span>
                            ))}
                          </div>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          <div className="pagination">
            <span className="pagination-info">
              {start + 1}–{Math.min(start + pageSize, filtered.length)} of {filtered.length}
              {atWindow && " · most recent 500 events"}
            </span>
            <div className="pagination-pages">
              <button
                className="page-btn"
                onClick={() => setPage(current - 1)}
                disabled={current <= 1}
              >
                Prev
              </button>
              {pageWindow(current, pageCount).map((p, i) =>
                p === "gap" ? (
                  <span key={`gap-${i}`} className="page-gap">
                    …
                  </span>
                ) : (
                  <button
                    key={p}
                    className={`page-btn${p === current ? " is-active" : ""}`}
                    onClick={() => setPage(p)}
                  >
                    {p}
                  </button>
                ),
              )}
              <button
                className="page-btn"
                onClick={() => setPage(current + 1)}
                disabled={current >= pageCount}
              >
                Next
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
