import { type CSSProperties, useEffect, useMemo, useRef, useState } from "react";
import {
  certificateDownloadUrl,
  deleteCertificate,
  generateCertificate,
  listCertificates,
  screenFile,
} from "../api";
import { MOCK_CERTIFICATES, MOCK_SCREEN_RESULT } from "../mock";
import type { CertificateEntry, ScreenResult } from "../types";

// The Sanction Screener tab: staff upload a client name list (CSV/Excel — the
// monthly AML export works directly), it's screened in memory against the UN /
// SEMA / Public Safety Canada watchlists, the analyst adjudicates each potential
// match, and a Certificate of Search is generated + stored to the audit archive.
// Matching is deterministic (no client data passes through the AI model) and the
// uploaded file is never persisted.

const ACCEPT = ".csv,.xlsx,.xls";

export function ScreenerPanel({
  apiBase,
  token,
  mock,
}: {
  apiBase: string;
  token?: string;
  mock: boolean;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [result, setResult] = useState<ScreenResult | null>(null);
  const [statuses, setStatuses] = useState<string[]>([]);
  const [screening, setScreening] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [certificates, setCertificates] = useState<CertificateEntry[]>([]);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const data = mock ? MOCK_CERTIFICATES : await listCertificates(apiBase, token);
        if (alive) setCertificates(data);
      } catch {
        // The past-certificates list is best-effort; never block screening on it.
      }
    })();
    return () => {
      alive = false;
    };
  }, [apiBase, token, mock]);

  const pending = useMemo(() => statuses.filter((s) => s === "pending").length, [statuses]);
  const hasMatches = result != null && result.total_matches > 0;

  function pickFile(next: File | null) {
    setFile(next);
    setResult(null);
    setStatuses([]);
    setNotice(null);
    setError(null);
  }

  async function runScreen() {
    if (!file) return;
    setError(null);
    setNotice(null);
    setResult(null);
    setScreening(true);
    try {
      const data = mock ? MOCK_SCREEN_RESULT : await screenFile(apiBase, file, token);
      setResult(data);
      setStatuses(data.flagged_results.map(() => "pending"));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Screening failed");
    } finally {
      setScreening(false);
    }
  }

  function adjudicate(index: number, status: string) {
    setStatuses((prev) => prev.map((s, i) => (i === index ? status : s)));
  }

  async function generate() {
    if (!result || pending > 0) return;
    setError(null);
    setGenerating(true);
    const payload = {
      results: {
        total_screened: result.total_screened,
        flagged_results: result.flagged_results.map((r, i) => ({ ...r, status: statuses[i] })),
      },
      file_hash: result.file_hash,
      metadata: {
        original_filename: file?.name ?? "upload",
        names_screened: result.total_screened,
      },
    };
    try {
      if (mock) {
        window.alert("The Certificate of Search is generated and stored once connected to the live API.");
      } else {
        const url = await generateCertificate(apiBase, payload, token);
        window.open(url, "_blank", "noopener");
        setNotice("Certificate of Search generated and stored to the audit archive.");
        try {
          setCertificates(await listCertificates(apiBase, token));
        } catch {
          // best-effort refresh of the history list
        }
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Certificate generation failed");
    } finally {
      setGenerating(false);
    }
  }

  async function downloadCert(key?: string) {
    if (!key) return;
    if (mock) {
      window.alert("Downloads are available once connected to the live API.");
      return;
    }
    try {
      window.open(await certificateDownloadUrl(apiBase, key, token), "_blank", "noopener");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to open the certificate");
    }
  }

  async function removeCert(key?: string) {
    if (!key) return;
    if (mock) {
      window.alert("Deletion is available once connected to the live API.");
      return;
    }
    setError(null);
    setNotice(null);
    try {
      await deleteCertificate(apiBase, key, token);
      setCertificates((cs) => cs.filter((c) => c.certificate_key !== key));
      setNotice("Certificate deleted — the immutable audit copy is retained.");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to delete the certificate");
    }
  }

  return (
    <div className="reports">
      <div className="reports-head">
        <h1 className="reports-title">Sanction Screener</h1>
        <p className="reports-lede">
          Upload a client name list (CSV or Excel — the monthly AML export works directly) to screen
          against the UN Consolidated, Canada SEMA, and Public Safety Canada watchlists. Review each
          potential match, then generate a Certificate of Search. Matching is deterministic — no
          client data passes through the AI model — and the uploaded file is never stored.
        </p>
      </div>

      <div className="reports-controls">
        <label className="field field-grow">
          <span>Name list (.csv, .xlsx, .xls)</span>
          <input
            type="file"
            accept={ACCEPT}
            onChange={(e) => pickFile(e.target.files?.[0] ?? null)}
          />
        </label>
        <button className="composer-btn" onClick={runScreen} disabled={!file || screening}>
          {screening ? <span className="spinner" aria-hidden="true" /> : "Screen"}
        </button>
      </div>

      {error && <p className="banner banner-error">{error}</p>}
      {notice && <p className="banner banner-ok">{notice}</p>}

      {screening && (
        <div className="status-note">
          <span className="spinner" aria-hidden="true" />
          Screening against the watchlists…
        </div>
      )}

      {result && (
        <>
          <div className="screen-summary">
            <Stat label="Names screened" value={result.total_screened} />
            <Stat label="Cleared" value={result.clear_count} />
            <Stat
              label="Flagged"
              value={result.total_flagged_persons}
              tone={result.total_flagged_persons ? "warn" : undefined}
            />
            <Stat
              label="High confidence"
              value={result.high_confidence_count}
              tone={result.high_confidence_count ? "flag" : undefined}
            />
            <Stat
              label="Potential"
              value={result.potential_match_count}
              tone={result.potential_match_count ? "warn" : undefined}
            />
          </div>

          {!hasMatches && (
            <p className="banner banner-ok">
              No potential matches — all {result.total_screened} name(s) cleared. Generate the
              Certificate of Search to record the result.
            </p>
          )}

          {hasMatches && (
            <div className="table-scroll">
              <table className="matter-table">
                <thead>
                  <tr>
                    <th>Client</th>
                    <th>Watchlist match</th>
                    <th>Score</th>
                    <th className="col-action">Adjudication</th>
                  </tr>
                </thead>
                <tbody>
                  {result.flagged_results.map((r, i) => (
                    <tr key={`${r.client_name}-${r.government_id}-${i}`}>
                      <td>
                        <div className="matter-name">{r.client_name}</div>
                        <div className="matter-sub">{clientMeta(r)}</div>
                      </td>
                      <td>
                        <div className="matter-name">{r.matched_name}</div>
                        <div className="matter-sub">{matchMeta(r)}</div>
                      </td>
                      <td>
                        <span
                          className={`badge ${r.category_color === "red" ? "badge-high" : "badge-potential"}`}
                        >
                          {Math.round(r.score)}% · {r.category}
                        </span>
                      </td>
                      <td className="col-action">
                        <div className="adjudge">
                          <button
                            className={`adjudge-btn${statuses[i] === "confirmed" ? " is-confirmed" : ""}`}
                            onClick={() => adjudicate(i, "confirmed")}
                          >
                            Confirm
                          </button>
                          <button
                            className={`adjudge-btn${statuses[i] === "dismissed" ? " is-dismissed" : ""}`}
                            onClick={() => adjudicate(i, "dismissed")}
                          >
                            Dismiss
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="screen-actions">
            {hasMatches && pending > 0 && (
              <span className="matter-sub">{pending} match(es) awaiting adjudication</span>
            )}
            <button className="composer-btn" onClick={generate} disabled={generating || pending > 0}>
              {generating ? <span className="spinner" aria-hidden="true" /> : "Generate Certificate of Search"}
            </button>
          </div>
        </>
      )}

      {certificates.length > 0 && (
        <div className="screen-history">
          <h2 className="screen-history-title">Certificates of Search</h2>
          <div className="table-scroll">
            <table className="matter-table">
              <thead>
                <tr>
                  <th>Generated</th>
                  <th>Result</th>
                  <th>Reviewed by</th>
                  <th className="col-action">Certificate</th>
                </tr>
              </thead>
              <tbody>
                {certificates.map((c, i) => (
                  <tr key={c.certificate_key ?? i}>
                    <td>
                      <div className="matter-name">{shortDateTime(c.generated_at)}</div>
                      <div className="matter-sub">
                        {c.total_screened ?? 0} screened · {c.confirmed_count ?? 0} confirmed ·{" "}
                        {c.dismissed_count ?? 0} dismissed
                      </div>
                    </td>
                    <td>
                      <span className="badge badge-review">
                        {c.review_status === "confirmed_matches" ? "Confirmed matches" : "Cleared"}
                      </span>
                    </td>
                    <td className="matter-sub">{c.reviewed_by ?? "—"}</td>
                    <td className="col-action">
                      <div className="cert-actions">
                        <button
                          className="link-btn"
                          onClick={() => downloadCert(c.certificate_key)}
                          disabled={!c.certificate_key}
                        >
                          Download PDF
                        </button>
                        <HoldToDelete
                          disabled={!c.certificate_key}
                          onConfirm={() => removeCert(c.certificate_key)}
                        />
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

// Press-and-hold to delete: a ~1.4s deliberate hold (clip-path fill sweeps across),
// snapping back fast if released early. Deliberate friction so a certificate is never
// removed by a stray click.
function HoldToDelete({
  onConfirm,
  disabled,
  holdMs = 1400,
}: {
  onConfirm: () => void | Promise<void>;
  disabled?: boolean;
  holdMs?: number;
}) {
  const [holding, setHolding] = useState(false);
  const timer = useRef<number | null>(null);

  function clear() {
    if (timer.current !== null) {
      window.clearTimeout(timer.current);
      timer.current = null;
    }
  }
  useEffect(() => clear, []);

  function start() {
    if (disabled || timer.current !== null) return;
    setHolding(true);
    timer.current = window.setTimeout(() => {
      timer.current = null;
      setHolding(false);
      void onConfirm();
    }, holdMs);
  }
  function cancel() {
    clear();
    setHolding(false);
  }

  return (
    <button
      type="button"
      className={`hold-delete${holding ? " is-holding" : ""}`}
      style={{ "--hold-ms": `${holdMs}ms` } as CSSProperties}
      disabled={disabled}
      onPointerDown={start}
      onPointerUp={cancel}
      onPointerLeave={cancel}
      onPointerCancel={cancel}
      title="Press and hold to delete"
    >
      <span className="hold-delete-fill" aria-hidden="true" />
      <span className="hold-delete-label">{holding ? "Keep holding…" : "Hold to delete"}</span>
    </button>
  );
}

function Stat({ label, value, tone }: { label: string; value: number; tone?: "warn" | "flag" }) {
  return (
    <div className={`screen-stat${tone ? ` is-${tone}` : ""}`}>
      <div className="screen-stat-value">{value}</div>
      <div className="screen-stat-label">{label}</div>
    </div>
  );
}

function clientMeta(r: { role: string; matter_number: string; client_dob?: string | null }): string {
  const parts = [r.role];
  if (r.matter_number && r.matter_number !== "N/A") parts.push(r.matter_number);
  if (r.client_dob && r.client_dob !== "Unknown") parts.push(`DOB ${r.client_dob}`);
  return parts.join(" · ");
}

function matchMeta(r: { source_list: string; government_id?: string; sanction_dob?: string }): string {
  const parts = [sourceLabel(r.source_list)];
  if (r.government_id) parts.push(r.government_id);
  if (r.sanction_dob && r.sanction_dob !== "Unknown") parts.push(`DOB ${r.sanction_dob}`);
  return parts.join(" · ");
}

function sourceLabel(source: string): string {
  if (source === "UN") return "UN Consolidated";
  if (source === "Canada-SEMA") return "Canada SEMA";
  if (source.includes("Public Safety")) return "Public Safety Canada";
  return source;
}

function shortDateTime(iso?: string): string {
  if (!iso) return "Certificate";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}
