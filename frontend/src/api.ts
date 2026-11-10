// Matter-search API client. Search is async (POST /search → poll GET
// /search/{id}) because fan-out + synthesis can exceed API Gateway's 29s.

import type {
  ActivityEntry,
  AmlExportEntry,
  AmlSchedule,
  CertificateEntry,
  ConnectorActionResult,
  IntegrationsHealth,
  Matter,
  MatterSelection,
  ReportEntry,
  ScreenResult,
  SearchJobSummary,
  SearchResult,
  SearchScope,
} from "./types";

const POLL_INTERVAL_MS = 2000;
// Wait for the full worker window: a heavy matter's fan-out + synthesis can take
// several minutes (accuracy-first), matching the search worker's Lambda timeout.
const POLL_TIMEOUT_MS = 900_000;

// Translate a raw backend/job error into calm, non-technical copy for the chat —
// staff should never see a botocore/stack string. Unknown errors get a generic
// line rather than the raw text (the original is still in the job record + logs).
export function friendlyError(raw?: string): string {
  const s = (raw || "").toLowerCase();
  if (s.includes("timed out") || s.includes("timeout"))
    return "The search took too long to finish — this can happen on very large matters. Please try again.";
  if (
    s.includes("throttl") ||
    s.includes("overload") ||
    s.includes("serviceunavailable") ||
    s.includes("too many requests") ||
    s.includes("connection failed")
  )
    return "The AI service was busy. Please try again in a moment.";
  if (s.includes("converse failed") || s.includes("bedrock"))
    return "The AI service had a problem generating this summary. Please try again.";
  return "Something went wrong running this search. Please try again.";
}

function authHeaders(token?: string): HeadersInit {
  const h: HeadersInit = { "Content-Type": "application/json" };
  if (token) h["Authorization"] = `Bearer ${token}`;
  return h;
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export async function search(
  base: string,
  query: string,
  token?: string,
  selection?: MatterSelection,
  full?: boolean,
  scope?: SearchScope,
  onProgress?: (phase: string) => void,
): Promise<SearchResult> {
  const body: Record<string, unknown> = { query };
  if (selection) body.selection = selection;
  if (full) body.full = true; // force the full status file (skip Tier-0 quick answer)
  if (scope && scope !== "all") body.scope = scope; // e.g. "clio" — skip Estateably in resolution
  const start = await fetch(`${base}/search`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify(body),
  });
  if (!start.ok) throw new Error(`Search failed (${start.status})`);
  const { job_id: jobId } = await start.json();

  const deadline = Date.now() + POLL_TIMEOUT_MS;
  while (Date.now() < deadline) {
    await sleep(POLL_INTERVAL_MS);
    const poll = await fetch(`${base}/search/${jobId}`, { headers: authHeaders(token) });
    if (!poll.ok) throw new Error(`Status check failed (${poll.status})`);
    const job = await poll.json();
    if (job.status === "done") return job.result as SearchResult;
    if (job.status === "error") throw new Error(job.error || "Search job failed");
    if (job.phase && onProgress) onProgress(job.phase as string);
  }
  throw new Error("Timed out waiting for the search to finish");
}

// The caller's recent searches (in-flight + completed), newest-first — backs the
// notification bell and the History tab. Scoped server-side to the caller's own
// jobs; empty when unauthenticated.
export async function listSearches(base: string, token?: string): Promise<SearchJobSummary[]> {
  const res = await fetch(`${base}/search`, { headers: authHeaders(token) });
  if (!res.ok) throw new Error(`Failed to load searches (${res.status})`);
  return (await res.json()).searches as SearchJobSummary[];
}

// Fetch one completed search (its stored status file) to re-open from history.
export async function getSearch(base: string, jobId: string, token?: string): Promise<SearchResult> {
  const res = await fetch(`${base}/search/${jobId}`, { headers: authHeaders(token) });
  if (!res.ok) throw new Error(`Failed to load search (${res.status})`);
  const job = await res.json();
  if (job.status === "error") throw new Error(job.error || "Search failed");
  if (job.status !== "done") throw new Error("That search is still running.");
  return job.result as SearchResult;
}

// Remove a stale/errored search from the caller's own history list. The
// underlying chain-of-custody audit trail (search.completed) is untouched —
// this only clears the convenience list entry.
export async function deleteSearch(base: string, jobId: string, token?: string): Promise<void> {
  const res = await fetch(`${base}/search/${jobId}`, {
    method: "DELETE",
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(`Failed to delete search (${res.status})`);
}

// Connector-health signal for the in-app banner: source errors (e.g. a dead
// QuickBooks token) from the caller's most recent full search. Never throws — a
// health check must not break the UI.
export async function getConnectorHealth(base: string, token?: string): Promise<string[]> {
  try {
    const res = await fetch(`${base}/connector-health`, { headers: authHeaders(token) });
    if (!res.ok) return [];
    return ((await res.json()).source_errors as string[]) ?? [];
  } catch {
    return [];
  }
}

// Integration health for the Integrations tab: live per-connector status + a
// downtime log, from the scheduled probe. Never throws — a health check must not
// break the UI; an empty snapshot is returned on any failure.
export async function getIntegrationsHealth(
  base: string,
  token?: string,
): Promise<IntegrationsHealth> {
  const empty: IntegrationsHealth = { integrations: [], log: [], alerting: false };
  try {
    const res = await fetch(`${base}/integrations`, { headers: authHeaders(token) });
    if (!res.ok) return empty;
    const data = (await res.json()) as Partial<IntegrationsHealth>;
    return {
      integrations: data.integrations ?? [],
      log: data.log ?? [],
      alerting: Boolean(data.alerting),
    };
  } catch {
    return empty;
  }
}

// Modular architecture (Phase 2): which modules/connectors this deployment has
// enabled. Never throws — an "everything enabled" default on any failure means
// a transient fetch error never hides a tab/row that should be visible.
export interface Capabilities {
  modules: { quarterly_reports: boolean; aml_export: boolean; sanction_screener: boolean };
  connectors: { clio: boolean; estateably: boolean; quickbooks: boolean; graph: boolean };
}

export async function fetchCapabilities(base: string, token?: string): Promise<Capabilities> {
  const allEnabled: Capabilities = {
    modules: { quarterly_reports: true, aml_export: true, sanction_screener: true },
    connectors: { clio: true, estateably: true, quickbooks: true, graph: true },
  };
  try {
    const res = await fetch(`${base}/capabilities`, { headers: authHeaders(token) });
    if (!res.ok) return allEnabled;
    return (await res.json()) as Capabilities;
  } catch {
    return allEnabled;
  }
}

// -- Quarterly report drafts -------------------------------------------------

export async function listReports(base: string, token?: string): Promise<ReportEntry[]> {
  const res = await fetch(`${base}/reports`, { headers: authHeaders(token) });
  if (!res.ok) throw new Error(`Failed to load reports (${res.status})`);
  const { reports } = await res.json();
  return reports as ReportEntry[];
}

export async function reportDownloadUrl(
  base: string,
  key: string,
  token?: string,
): Promise<string> {
  const res = await fetch(`${base}/reports/download?key=${encodeURIComponent(key)}`, {
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(`Failed to get download link (${res.status})`);
  const { url } = await res.json();
  return url as string;
}

// -- Activity Logs (chain-of-custody audit trail) ----------------------------

export async function listActivity(
  base: string,
  token?: string,
  limit?: number,
): Promise<ActivityEntry[]> {
  const q = limit ? `?limit=${limit}` : "";
  const res = await fetch(`${base}/activity${q}`, { headers: authHeaders(token) });
  if (!res.ok) throw new Error(`Failed to load activity (${res.status})`);
  const { activity } = await res.json();
  return activity as ActivityEntry[];
}

// -- Monthly AML name export --------------------------------------------------

export async function listAmlExports(base: string, token?: string): Promise<AmlExportEntry[]> {
  const res = await fetch(`${base}/aml-exports`, { headers: authHeaders(token) });
  if (!res.ok) throw new Error(`Failed to load AML exports (${res.status})`);
  const { exports: items } = await res.json();
  return items as AmlExportEntry[];
}

export async function amlExportDownloadUrl(
  base: string,
  key: string,
  token?: string,
): Promise<string> {
  const res = await fetch(`${base}/aml-exports/download?key=${encodeURIComponent(key)}`, {
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(`Failed to get download link (${res.status})`);
  const { url } = await res.json();
  return url as string;
}

// On-demand "run now" — async-triggers the AML export Lambda; poll listAmlExports.
export async function generateAmlExport(base: string, token?: string): Promise<void> {
  const res = await fetch(`${base}/aml-exports/generate`, {
    method: "POST",
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(`Failed to start the AML export (${res.status})`);
}

export async function getAmlSchedule(base: string, token?: string): Promise<AmlSchedule> {
  const res = await fetch(`${base}/aml-exports/schedule`, { headers: authHeaders(token) });
  if (!res.ok) throw new Error(`Failed to load the schedule (${res.status})`);
  return (await res.json()) as AmlSchedule;
}

export async function updateAmlSchedule(
  base: string,
  body: { enabled: boolean; day_of_month: number; hour: number; minute: number },
  token?: string,
): Promise<AmlSchedule> {
  const res = await fetch(`${base}/aml-exports/schedule`, {
    method: "PUT",
    headers: authHeaders(token),
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.error || `Failed to save the schedule (${res.status})`);
  }
  return (await res.json()) as AmlSchedule;
}

// -- Quarterly matters / on-demand generation --------------------------------

export async function listMatters(base: string, token?: string): Promise<Matter[]> {
  const res = await fetch(`${base}/matters`, { headers: authHeaders(token) });
  if (!res.ok) throw new Error(`Failed to load matters (${res.status})`);
  const { matters } = await res.json();
  return matters as Matter[];
}

export async function generateReport(
  base: string,
  body: { matter_id: string; year: number; quarter: number },
  token?: string,
): Promise<void> {
  const res = await fetch(`${base}/reports/generate`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`Failed to start generation (${res.status})`);
}

// -- Sanction Screener --------------------------------------------------------
// The uploaded file is sent as the raw request body (filename in X-File-Name);
// the API parses + screens it in memory and returns only the flagged matches.

export async function screenFile(base: string, file: File, token?: string): Promise<ScreenResult> {
  const headers: Record<string, string> = { "X-File-Name": file.name };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(`${base}/screener/screen`, { method: "POST", headers, body: file });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.error || `Screening failed (${res.status})`);
  }
  return (await res.json()) as ScreenResult;
}

export async function generateCertificate(
  base: string,
  payload: { results: unknown; file_hash: string; metadata?: Record<string, unknown> },
  token?: string,
): Promise<string> {
  const res = await fetch(`${base}/screener/certificate`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.error || `Certificate generation failed (${res.status})`);
  }
  const { url } = await res.json();
  return url as string;
}

export async function listCertificates(base: string, token?: string): Promise<CertificateEntry[]> {
  const res = await fetch(`${base}/screener/certificates`, { headers: authHeaders(token) });
  if (!res.ok) throw new Error(`Failed to load certificates (${res.status})`);
  const { certificates } = await res.json();
  return certificates as CertificateEntry[];
}

export async function certificateDownloadUrl(
  base: string,
  key: string,
  token?: string,
): Promise<string> {
  const res = await fetch(`${base}/screener/certificates/download?key=${encodeURIComponent(key)}`, {
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(`Failed to get download link (${res.status})`);
  const { url } = await res.json();
  return url as string;
}

// Soft-delete: the server drops a delete marker (the immutable version is retained).
export async function deleteCertificate(base: string, key: string, token?: string): Promise<void> {
  const res = await fetch(`${base}/screener/certificates?key=${encodeURIComponent(key)}`, {
    method: "DELETE",
    headers: authHeaders(token),
  });
  if (!res.ok) throw new Error(`Failed to delete certificate (${res.status})`);
}

// -- Connector onboarding / reconnect (admin-only Setup screen) --------------

export async function connectEstateably(
  base: string,
  body: { username: string; password: string; base_url?: string },
  token?: string,
): Promise<ConnectorActionResult> {
  const res = await fetch(`${base}/connectors/estateably`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.error || `Failed to connect Estateably (${res.status})`);
  }
  return (await res.json()) as ConnectorActionResult;
}

export async function connectGraph(
  base: string,
  body: { tenant_id: string; client_id: string; client_secret: string },
  token?: string,
): Promise<ConnectorActionResult> {
  const res = await fetch(`${base}/connectors/graph`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.error || `Failed to connect Graph (${res.status})`);
  }
  return (await res.json()) as ConnectorActionResult;
}

export async function clioAuthorizeUrl(
  base: string,
  body: { client_id: string; base_url?: string },
  token?: string,
): Promise<{ url: string; state: string }> {
  const res = await fetch(`${base}/connectors/clio/authorize-url`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.error || `Failed to build the Clio authorize link (${res.status})`);
  }
  return (await res.json()) as { url: string; state: string };
}

export async function clioExchange(
  base: string,
  body: { client_id: string; client_secret: string; code: string; base_url?: string },
  token?: string,
): Promise<ConnectorActionResult> {
  const res = await fetch(`${base}/connectors/clio/exchange`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.error || `Failed to connect Clio (${res.status})`);
  }
  return (await res.json()) as ConnectorActionResult;
}

export async function quickbooksStart(
  base: string,
  body: { client_id: string; client_secret: string; environment: string },
  token?: string,
): Promise<{ url: string }> {
  const res = await fetch(`${base}/connectors/quickbooks/start`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.error || `Failed to connect QuickBooks (${res.status})`);
  }
  return (await res.json()) as { url: string };
}
