import { useCallback, useEffect, useMemo, useState } from "react";
import { generateReport, listMatters, listReports, reportDownloadUrl } from "../api";
import { MOCK_MATTERS, MOCK_REPORTS } from "../mock";
import type { Matter, ReportEntry } from "../types";

// The Quarterly Reports tab. Until the quarterly schedule is switched on, this is
// an on-demand tool: it lists open matters and lets staff generate a draft per
// matter for a chosen quarter, then review/download it. Read-only outputs —
// nothing is sent to clients.

const QUARTERS = [1, 2, 3, 4] as const;
const POLL_MS = 4000;
const POLL_TIMEOUT_MS = 90_000;

/** The most recently completed calendar quarter (what a quarterly run reports on). */
function previousQuarter(now: Date): { year: number; quarter: number } {
  const q = Math.floor(now.getMonth() / 3) + 1;
  return q === 1 ? { year: now.getFullYear() - 1, quarter: 4 } : { year: now.getFullYear(), quarter: q - 1 };
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

type ReviewFlag = { label: string; className: string; title: string };

/** Attention pills for a draft — the same review items as the docx checklist, so
 *  staff can triage from the list: unclassified account(s) (red), a stale
 *  real-property valuation, and the pending outside-counsel portfolio total (amber). */
function reviewFlags(report: ReportEntry): ReviewFlag[] {
  const flags: ReviewFlag[] = [];
  const unclassified = report.unclassified_accounts ?? [];
  if (unclassified.length > 0) {
    flags.push({
      label: `${unclassified.length} unclassified`,
      className: "badge-high",
      title: `Shown under Cash — verify category: ${unclassified.join(", ")}`,
    });
  }
  const stale = report.stale_valuations ?? [];
  if (stale.length > 0) {
    flags.push({
      label: "Stale valuation",
      className: "badge-potential",
      title: `Real-property value over 15 months old: ${stale
        .map((s) => `${s.account} (as at ${s.as_at})`)
        .join("; ")}`,
    });
  }
  if (report.investment_pending) {
    flags.push({
      label: "Portfolio pending",
      className: "badge-potential",
      title: "Enter the outside-counsel investment portfolio total before sending.",
    });
  }
  return flags;
}

export function ReportsPanel({
  apiBase,
  token,
  mock,
}: {
  apiBase: string;
  token?: string;
  mock: boolean;
}) {
  const [matters, setMatters] = useState<Matter[] | null>(null);
  const [reports, setReports] = useState<ReportEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [period, setPeriod] = useState(() => previousQuarter(new Date()));
  const [busy, setBusy] = useState<Set<string>>(new Set());

  const periodCode = `${period.year}-Q${period.quarter}`;

  const loadReports = useCallback(async () => {
    const data = mock ? MOCK_REPORTS : await listReports(apiBase, token);
    setReports(data);
    return data;
  }, [apiBase, token, mock]);

  useEffect(() => {
    let alive = true;
    setError(null);
    (async () => {
      try {
        const [m] = await Promise.all([
          mock ? Promise.resolve(MOCK_MATTERS) : listMatters(apiBase, token),
          loadReports(),
        ]);
        if (alive) setMatters(m);
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : "Failed to load");
      }
    })();
    return () => {
      alive = false;
    };
  }, [apiBase, token, mock, loadReports]);

  // Report for a matter in the selected quarter (status lookup).
  const reportFor = useCallback(
    (m: Matter): ReportEntry | undefined =>
      reports.find((r) => r.matter_number === m.matter_number && r.period === periodCode),
    [reports, periodCode],
  );

  async function download(key?: string) {
    if (!key) return;
    if (mock) {
      window.alert("Downloads are available once connected to the live API.");
      return;
    }
    try {
      window.open(await reportDownloadUrl(apiBase, key, token), "_blank", "noopener");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to open the document");
    }
  }

  async function generate(m: Matter) {
    if (mock) {
      window.alert("On-demand generation runs once connected to the live API.");
      return;
    }
    setError(null);
    setBusy((b) => new Set(b).add(m.matter_number));
    try {
      await generateReport(apiBase, { matter_id: m.matter_id, year: period.year, quarter: period.quarter }, token);
      const deadline = Date.now() + POLL_TIMEOUT_MS;
      while (Date.now() < deadline) {
        await sleep(POLL_MS);
        const data = await loadReports();
        if (data.some((r) => r.matter_number === m.matter_number && r.period === periodCode)) break;
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Generation failed");
    } finally {
      setBusy((b) => {
        const next = new Set(b);
        next.delete(m.matter_number);
        return next;
      });
    }
  }

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!matters) return [];
    if (!q) return matters;
    return matters.filter(
      (m) =>
        m.client_name.toLowerCase().includes(q) ||
        m.description.toLowerCase().includes(q) ||
        m.matter_number.toLowerCase().includes(q),
    );
  }, [matters, search]);

  return (
    <div className="reports">
      <div className="reports-head">
        <h1 className="reports-title">Quarterly Reports</h1>
        <p className="reports-lede">
          Generate a draft quarterly trust statement for any matter, then review and correct it
          — including the investment total from the custodian statement — before sending. Nothing
          is sent automatically.
        </p>
        <p className="reports-pilot">
          On-demand (pilot): generation is manual per matter until the automatic quarterly schedule
          is switched on.
        </p>
      </div>

      <div className="reports-controls">
        <label className="field">
          <span>Quarter</span>
          <div className="period-select">
            <select
              value={period.quarter}
              onChange={(e) => setPeriod((p) => ({ ...p, quarter: Number(e.target.value) }))}
            >
              {QUARTERS.map((q) => (
                <option key={q} value={q}>
                  Q{q}
                </option>
              ))}
            </select>
            <select
              value={period.year}
              onChange={(e) => setPeriod((p) => ({ ...p, year: Number(e.target.value) }))}
            >
              {[period.year + 1, period.year, period.year - 1, period.year - 2].map((y) => (
                <option key={y} value={y}>
                  {y}
                </option>
              ))}
            </select>
          </div>
        </label>
        <label className="field field-grow">
          <span>Search</span>
          <input
            type="search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Filter by client, matter number, or description"
          />
        </label>
      </div>

      {error && <p className="banner banner-error">{error}</p>}

      {!matters && !error && (
        <div className="status-note">
          <span className="spinner" aria-hidden="true" />
          Loading matters…
        </div>
      )}

      {matters && filtered.length === 0 && (
        <p className="banner banner-warn">No matters match “{search}”.</p>
      )}

      {matters && filtered.length > 0 && (
        <div className="table-scroll">
          <table className="matter-table">
            <thead>
              <tr>
                <th>Client / Matter</th>
                <th>Status — {periodCode}</th>
                <th className="col-action">Action</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((m) => {
                const report = reportFor(m);
                const generating = busy.has(m.matter_number);
                return (
                  <tr key={m.matter_id}>
                    <td>
                      <div className="matter-name">{m.client_name || m.description}</div>
                      <div className="matter-sub">
                        {m.matter_number}
                        {m.client_name && m.description ? ` · ${m.description}` : ""}
                      </div>
                    </td>
                    <td>
                      {generating ? (
                        <span className="status-note">
                          <span className="spinner" aria-hidden="true" />
                          Generating…
                        </span>
                      ) : report ? (
                        <div className="report-ready">
                          <span className="badge badge-review">Draft ready</span>
                          {reviewFlags(report).map((f) => (
                            <span key={f.label} className={`badge ${f.className}`} title={f.title}>
                              {f.label}
                            </span>
                          ))}
                          <button className="link-btn ghost" onClick={() => download(report.files.cover_letter)}>
                            Cover letter
                          </button>
                          <button className="link-btn ghost" onClick={() => download(report.files.trust_ledger)}>
                            Trust ledger
                          </button>
                        </div>
                      ) : (
                        <span className="matter-sub">Not generated</span>
                      )}
                    </td>
                    <td className="col-action">
                      <button className="link-btn" onClick={() => generate(m)} disabled={generating}>
                        {report ? "Regenerate" : "Generate"}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
