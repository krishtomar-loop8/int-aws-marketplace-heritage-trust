// API response shapes (mirror backend/api/pipeline.py).

export interface Citation {
  system: string;
  record_id: string;
  note?: string;
  url?: string; // deep-link to the source record, when available
  // In-app evidence for Outlook citations: the masked body excerpt + Pacific date let a
  // reader verify a cited email inside the report, since the deep-link (url) only opens
  // for someone with access to that message's mailbox.
  excerpt?: string;
  date?: string;
}

export interface Ref {
  system: string;
  id: string;
  label: string;
  // Clio refs only: the client-contact id QuickBooks keys its customer on. Must
  // ride through the disambiguation round-trip untouched — the backend needs it
  // back on the selection for reconciliation (multi-matter clients especially).
  qb_customer_id?: string | null;
}

export interface Candidate {
  name: string;
  matter_type: string | null;
  score: number;
  systems: string[];
  refs: Ref[];
}

// A candidate the user picked from a disambiguation, sent back to /search so the
// backend resolves to that exact matter instead of re-matching the plain name.
export interface MatterSelection {
  name: string;
  matter_type: string | null;
  refs: Ref[];
}

// Which sources resolve the matter. "all" = Estateably + Clio (default); "clio"
// skips Estateably so an old Clio + SharePoint matter (no Estateably record)
// surfaces instead of being outranked. Mirrors the backend `scope` param.
export type SearchScope = "all" | "clio";

export interface Answer {
  sections: Record<string, string>;
  citations: Citation[];
  invalid_citations: Citation[];
  sources_reviewed: string[];
  insufficient_info: boolean;
  // The dossier errors behind insufficient_info (deterministic, from the
  // backend) — the banner shows the first one so staff see WHY, not a generic
  // warning. Optional: absent on results stored before the field existed.
  insufficient_reasons?: string[];
  missing_sections: string[];
  parse_ok: boolean;
}

// Tier-0 quick answer: a deterministic, no-LLM reply to a narrow question.
export interface QuickAnswer {
  intent: string;
  text: string;
  citations: Citation[];
}

export interface SearchResult {
  status: "ok" | "disambiguation" | "not_found" | "answer";
  query?: string;
  term?: string;
  matter?: { name: string; type: string | null; refs: Ref[] };
  answer?: Answer;
  quick?: QuickAnswer; // present when status === "answer"
  candidates?: Candidate[];
  dossier_errors?: string[];
  usage?: { model: string; input_tokens: number; output_tokens: number };
}

// One turn in the Matter Search thread. Persisted to sessionStorage (minus the
// transient "loading" turn) so the conversation survives refresh and module
// navigation. See lib/threadStore.ts.
export type Message =
  | { role: "user"; text: string }
  | { role: "assistant"; kind: "loading"; phase?: string }
  | { role: "assistant"; kind: "result"; result: SearchResult; query?: string }
  // The failing request is retained on the error turn so the "Retry" button can
  // re-run the exact same search (query + disambiguation pick + full-report flag).
  | {
      role: "assistant";
      kind: "error";
      text: string;
      query?: string;
      selection?: MatterSelection;
      full?: boolean;
      scope?: SearchScope;
    };

// A row in the per-user search history / notification list (GET /search).
// The full result is fetched on demand (GET /search/{job_id}) when re-opened.
export interface SearchJobSummary {
  job_id: string;
  status: "pending" | "done" | "error";
  query?: string;
  matter_name?: string;
  created_at?: number; // epoch seconds
  phase?: string; // current phase while pending
}

// An open Clio matter, for the on-demand quarterly generation picker.
export interface Matter {
  matter_id: string;
  matter_number: string;
  description: string;
  client_name: string;
}

// A real-property account whose recorded value is over 15 months old (Decision D).
export interface StaleValuation {
  account: string;
  as_at: string; // ISO date, or "unknown"
}

// A generated quarterly report draft (mirrors the manifest.json the D5 Lambda
// writes; see backend/lambdas/quarterly_report/storage.py).
export interface ReportEntry {
  matter_number: string;
  trust_name?: string;
  period: string; // e.g. "2026-Q1"
  period_start?: string;
  period_end?: string;
  generated_at?: string;
  review_status?: string;
  cash_balance?: number | null;
  investment_pending?: boolean;
  total_assets?: number | null;
  // Assets Held breakdown + review flags (Nicole's Decisions A/D/E).
  asset_breakdown?: Record<string, number | null>;
  unclassified_accounts?: string[];
  stale_valuations?: StaleValuation[];
  real_property_as_at?: string | null;
  files: { cover_letter?: string; trust_ledger?: string; manifest?: string };
}

// The AML export's monthly EventBridge schedule (GET/PUT /aml-exports/schedule;
// see backend/api/aml_admin.py). day_of_month is capped at 28.
export interface AmlSchedule {
  enabled: boolean;
  day_of_month: number;
  hour: number;
  minute: number;
  timezone: string;
}

// A generated monthly AML name-export run (mirrors the manifest.json the D6
// Lambda writes; see backend/lambdas/aml_export/storage.py).
export interface AmlExportEntry {
  period: string; // e.g. "2026-07"
  generated_at?: string;
  review_status?: string;
  parties_count?: number;
  excluded_count?: number;
  excluded_parties_count?: number;
  roster_incomplete_count?: number;
  exceptions_count?: number;
  workbook_key?: string;
}

// Sanction Screener — one flagged match returned by POST /screener/screen
// (mirrors backend/screener/screening.py). Exception-only: clean names are
// never returned. `status` starts "pending"; the analyst sets confirmed/dismissed.
export interface FlaggedMatch {
  client_name: string;
  client_dob?: string | null;
  matter_number: string;
  role: string;
  source_tab: string;
  matched_name: string;
  full_name: string;
  score: number;
  category: string; // "High Confidence" | "Potential Match"
  category_color: string; // "red" | "orange"
  source_list: string;
  government_id?: string;
  sanction_dob?: string;
  dob_match?: boolean;
  status: string;
}

// The screen response: summary counts (no names) + the flagged matches + the
// SHA-256 of the uploaded file (echoed back with the certificate request).
export interface ScreenResult {
  total_screened: number;
  total_flagged_persons: number;
  total_matches: number;
  high_confidence_count: number;
  potential_match_count: number;
  clear_count: number;
  file_hash: string;
  flagged_results: FlaggedMatch[];
}

// A stored Certificate of Search (mirrors the PII-free manifest the backend
// writes; see backend/api/screener.py). Counts + hash + reviewer only.
export interface CertificateEntry {
  generated_at?: string;
  review_status?: string; // "cleared" | "confirmed_matches"
  total_screened?: number;
  initial_matches?: number;
  confirmed_count?: number;
  dismissed_count?: number;
  file_hash?: string;
  reviewed_by?: string | null;
  certificate_key?: string;
}

// One chain-of-custody activity row (mirrors backend/api/activity.py). Records
// who did what, when, and — for searches — which source systems were read.
export interface ActivityEntry {
  id?: string;
  timestamp?: string; // ISO8601 UTC
  actor_email?: string;
  actor_sub?: string;
  action: string; // "matter.search" | "report.generate" | "aml.run" | "aml.schedule"
  //         | "screener.screen" | "screener.certificate" | "screener.delete" | "download"
  summary: string; // layman one-liner composed server-side
  target?: string; // matter / period / filename
  sources?: string[]; // source systems read (searches only)
  detail?: Record<string, unknown>;
  status?: string; // "success" | "error"
}

// Integration health (scheduled connector probe; mirrors backend/api/integrations.py).
// The current status of one connected system, from the most recent health probe.
export interface IntegrationStatus {
  integration: string; // "clio" | "estateably" | "graph" | "quickbooks"
  ok: boolean;
  checked_at?: string; // ISO8601 UTC
  consecutive_failures: number; // ≥ 2 → the UI shows a pulsing alert
  detail?: string; // connector error string when down
  suggested_fix?: string; // plain-language remediation when down
}

// One up↔down change in the downtime log.
export interface IntegrationTransition {
  integration: string;
  ok: boolean; // the state entered at this transition (true = recovered)
  at?: string; // ISO8601 UTC
  detail?: string;
}

// GET /integrations — per-connector status + the recent downtime log.
export interface IntegrationsHealth {
  integrations: IntegrationStatus[];
  log: IntegrationTransition[];
  alerting: boolean; // any integration down for ≥ 2 consecutive checks
}

// The mandatory report sections, in order (mirrors framework.SECTION_KEYS).
export const SECTION_ORDER: { key: string; title: string }[] = [
  { key: "current_stage", title: "Current stage" },
  { key: "completed_items", title: "Completed items" },
  { key: "outlook_correspondence", title: "Outlook correspondence" },
  { key: "outstanding_from_correspondence", title: "Outstanding from correspondence" },
  { key: "kyc_intake", title: "KYC & client intake" },
  { key: "tax_status", title: "Tax & filing status" },
  { key: "assets_liabilities_banking", title: "Assets, liabilities & banking" },
  { key: "distribution_readiness", title: "Distribution readiness" },
  { key: "upcoming_deadlines", title: "Upcoming deadlines" },
  { key: "risks_blockers", title: "Risks & blockers" },
  { key: "sources_reviewed", title: "Sources reviewed" },
  { key: "suggested_next_action", title: "Suggested next action" },
];
