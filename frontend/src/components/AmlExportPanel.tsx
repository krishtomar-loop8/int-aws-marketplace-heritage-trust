import { useCallback, useEffect, useState } from "react";
import {
  amlExportDownloadUrl,
  generateAmlExport,
  getAmlSchedule,
  listAmlExports,
  updateAmlSchedule,
} from "../api";
import { MOCK_AML_EXPORTS, MOCK_AML_SCHEDULE } from "../mock";
import type { AmlExportEntry, AmlSchedule } from "../types";

// The AML Export tab: lists the monthly deduplicated active-client workbooks
// (Estates/Trusts/POA) generated from Clio + Estateably, for staff to review
// and screen in the in-app Sanction Screener tab. Nothing here is sent
// automatically, and this data never passes through an LLM. Staff can run the
// export on demand and set its monthly schedule from here.

const POLL_MS = 5000;
const POLL_TIMEOUT_MS = 240_000; // the export Lambda's own timeout is 300s
const MINUTES = [0, 15, 30, 45];
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const currentPeriod = () => new Date().toISOString().slice(0, 7); // "YYYY-MM"

export function AmlExportPanel({
  apiBase,
  token,
  mock,
}: {
  apiBase: string;
  token?: string;
  mock: boolean;
}) {
  const [exports, setExports] = useState<AmlExportEntry[] | null>(null);
  const [schedule, setSchedule] = useState<AmlSchedule | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [savingSchedule, setSavingSchedule] = useState(false);
  const [running, setRunning] = useState(false);

  const loadExports = useCallback(async () => {
    const data = mock ? MOCK_AML_EXPORTS : await listAmlExports(apiBase, token);
    setExports(data);
    return data;
  }, [apiBase, token, mock]);

  useEffect(() => {
    let alive = true;
    setError(null);
    setExports(null);
    setSchedule(null);
    (async () => {
      try {
        const [, sch] = await Promise.all([
          loadExports(),
          mock ? Promise.resolve(MOCK_AML_SCHEDULE) : getAmlSchedule(apiBase, token),
        ]);
        if (alive) setSchedule(sch);
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : "Failed to load AML exports");
      }
    })();
    return () => {
      alive = false;
    };
  }, [apiBase, token, mock, loadExports]);

  async function download(key?: string) {
    if (!key) return;
    if (mock) {
      window.alert("Downloads are available once connected to the live API.");
      return;
    }
    try {
      const url = await amlExportDownloadUrl(apiBase, key, token);
      window.open(url, "_blank", "noopener");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to open the workbook");
    }
  }

  function editSchedule(patch: Partial<AmlSchedule>) {
    setSchedule((s) => (s ? { ...s, ...patch } : s));
  }

  async function saveSchedule() {
    if (!schedule) return;
    setSavingSchedule(true);
    setError(null);
    setNotice(null);
    try {
      const body = {
        enabled: schedule.enabled,
        day_of_month: schedule.day_of_month,
        hour: schedule.hour,
        minute: schedule.minute,
      };
      const updated = mock ? { ...schedule } : await updateAmlSchedule(apiBase, body, token);
      setSchedule(updated);
      setNotice("Schedule saved.");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save the schedule");
    } finally {
      setSavingSchedule(false);
    }
  }

  async function runNow() {
    setRunning(true);
    setError(null);
    setNotice(null);
    try {
      if (mock) {
        window.alert("The export runs against Clio + Estateably once connected to the live API.");
        return;
      }
      const before = exports?.find((e) => e.period === currentPeriod())?.generated_at;
      await generateAmlExport(apiBase, token);
      setNotice(
        "Export started — it can take a couple of minutes. Staff are also emailed when it's ready.",
      );
      const deadline = Date.now() + POLL_TIMEOUT_MS;
      while (Date.now() < deadline) {
        await sleep(POLL_MS);
        const data = await loadExports();
        const now = data.find((e) => e.period === currentPeriod());
        if (now && now.generated_at !== before) {
          setNotice("Export complete — the new workbook is below.");
          return;
        }
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to run the export");
    } finally {
      setRunning(false);
    }
  }

  const minuteOptions = schedule && !MINUTES.includes(schedule.minute)
    ? [schedule.minute, ...MINUTES]
    : MINUTES;

  return (
    <div className="reports">
      <div className="reports-head">
        <h1 className="reports-title">AML Export</h1>
        <p className="reports-lede">
          Monthly deduplicated active-client roster (Estates / Trusts / POA) compiled from Clio
          and Estateably. Review the workbook, then screen it in the Sanction Screener tab —
          nothing here is sent automatically, and no client data passes through the AI model.
        </p>
        <p className="reports-caution">
          <strong>Always review this export before relying on it</strong> — it is an automated
          draft compiled from Clio and Estateably and must be human-verified.
        </p>
      </div>

      {error && <p className="banner banner-error">{error}</p>}
      {notice && <p className="banner banner-ok">{notice}</p>}

      {/* Schedule + on-demand run */}
      {schedule && (
        <div className="schedule-card">
          <div className="schedule-row">
            <p className="schedule-status">{describeSchedule(schedule)}</p>
            <button className="composer-btn" onClick={runNow} disabled={running}>
              {running ? <span className="spinner" aria-hidden="true" /> : "Run export now"}
            </button>
          </div>
          <div className="reports-controls">
            <label className="field">
              <span>Automatic monthly run</span>
              <select
                value={schedule.enabled ? "on" : "off"}
                onChange={(e) => editSchedule({ enabled: e.target.value === "on" })}
              >
                <option value="on">On</option>
                <option value="off">Off</option>
              </select>
            </label>
            <label className="field">
              <span>Day of month</span>
              <select
                value={schedule.day_of_month}
                disabled={!schedule.enabled}
                onChange={(e) => editSchedule({ day_of_month: Number(e.target.value) })}
              >
                {Array.from({ length: 28 }, (_, i) => i + 1).map((d) => (
                  <option key={d} value={d}>
                    {d}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>Time ({schedule.timezone || "local"})</span>
              <div className="period-select">
                <select
                  value={schedule.hour}
                  disabled={!schedule.enabled}
                  onChange={(e) => editSchedule({ hour: Number(e.target.value) })}
                >
                  {Array.from({ length: 24 }, (_, h) => h).map((h) => (
                    <option key={h} value={h}>
                      {String(h).padStart(2, "0")}
                    </option>
                  ))}
                </select>
                <select
                  value={schedule.minute}
                  disabled={!schedule.enabled}
                  onChange={(e) => editSchedule({ minute: Number(e.target.value) })}
                >
                  {minuteOptions.map((m) => (
                    <option key={m} value={m}>
                      {String(m).padStart(2, "0")}
                    </option>
                  ))}
                </select>
              </div>
            </label>
            <button className="link-btn ghost" onClick={saveSchedule} disabled={savingSchedule}>
              {savingSchedule ? "Saving…" : "Save schedule"}
            </button>
          </div>
        </div>
      )}

      {!exports && !error && (
        <div className="status-note">
          <span className="spinner" aria-hidden="true" />
          Loading AML exports…
        </div>
      )}

      {exports && exports.length === 0 && (
        <p className="banner banner-warn">
          No AML exports yet. Run one now, or a workbook appears here after the monthly run.
        </p>
      )}

      {exports && exports.length > 0 && (
        <ul className="report-list">
          {exports.map((e) => (
            <li className="report-card" key={e.period}>
              <div className="report-main">
                <div className="report-name">{prettyPeriod(e.period)}</div>
                <div className="report-meta">
                  <span>{e.parties_count ?? 0} parties</span>
                  <span className="dot">·</span>
                  <span>{e.excluded_parties_count ?? 0} excluded parties</span>
                  <span className="dot">·</span>
                  <span>{e.excluded_count ?? 0} excluded matters</span>
                  <span className="dot">·</span>
                  <span>{e.exceptions_count ?? 0} exceptions</span>
                  {e.generated_at && (
                    <>
                      <span className="dot">·</span>
                      <span>Generated {shortDate(e.generated_at)}</span>
                    </>
                  )}
                </div>
              </div>
              <div className="report-actions">
                <button
                  className="link-btn"
                  onClick={() => download(e.workbook_key)}
                  disabled={!e.workbook_key}
                >
                  Download workbook (.xlsx)
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function describeSchedule(s: AmlSchedule): string {
  if (!s.enabled) return "Automatic runs are off — the export only runs when you click “Run export now”.";
  const time = `${String(s.hour).padStart(2, "0")}:${String(s.minute).padStart(2, "0")}`;
  return `Runs automatically on day ${s.day_of_month} of each month at ${time} ${s.timezone}.`;
}

function prettyPeriod(period: string): string {
  const [year, month] = period.split("-");
  const names = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
  ];
  const index = Number(month) - 1;
  return index >= 0 && index < 12 ? `${names[index]} ${year}` : period;
}

function shortDate(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString();
}
