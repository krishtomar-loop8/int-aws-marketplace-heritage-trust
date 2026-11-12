// Sample data for mock mode (local design review / screenshots). Mirrors the
// early-administration estate example from the matter-search spec.

import type { ConfigResponse } from "./api";
import type {
  ActivityEntry,
  AmlExportEntry,
  AmlSchedule,
  CertificateEntry,
  IntegrationsHealth,
  Matter,
  ReportEntry,
  ScreenResult,
  SearchResult,
} from "./types";

export const MOCK_AML_SCHEDULE: AmlSchedule = {
  enabled: false,
  day_of_month: 1,
  hour: 0,
  minute: 0,
  timezone: "America/Vancouver",
};

// Deployment Config tab sample — a mix of "configured" and "using default"
// fields so both badge states render in mock mode.
export const MOCK_CONFIG: ConfigResponse = {
  fields: {
    org_name: { value: "Aphrodite Trust Company", source: "configured" },
    org_office_address: { value: "545 Clyde Avenue, West Vancouver", source: "default" },
    excluded_sharepoint_folders: { value: ["hr", "human resources", "finance"], source: "default" },
    own_company_names: { value: ["Aphrodite", "Aphrodite Trust Company"], source: "configured" },
    internal_title_keywords: {
      value: [
        "accounting",
        "bcfsa communication",
        "finance & compliance",
        "finance and compliance",
        "board of directors",
        "wills bank",
        "suspense",
        "natalie's files",
        "natalies files",
        "aphrodite - administration",
        "aphrodite administration",
      ],
      source: "default",
    },
    internal_matter_number_pattern: { value: "^\\s*(2100-\\d+|999999)\\b", source: "default" },
    letterhead_name: { value: "Aphrodite", source: "default" },
    letterhead_address: {
      value: "220-545 Clyde Avenue, West Vancouver, BC V7T 1C5 | 778.742.5005 | aphrodite.example",
      source: "default",
    },
    letterhead_signoff: { value: "APHRODITE", source: "default" },
    letterhead_privacy_notice: {
      value: "Our privacy practices are described at www.aphrodite.example/privacy.",
      source: "default",
    },
    quarterly_qualifying_practice_areas: {
      value: ["Estate Administration", "Trust Administration"],
      source: "configured",
    },
    quarterly_exclusion_field: { value: "Report Exclusion", source: "default" },
    sharepoint_sites: { value: ["Estates", "Trusts"], source: "configured" },
  },
};

export const MOCK_MATTERS: Matter[] = [
  { matter_id: "1", matter_number: "260012", description: "John Dalton Burgess Trust", client_name: "John Dalton Burgess" },
  { matter_id: "2", matter_number: "250043", description: "Estate of Wilfred Doerges", client_name: "Wilfred Doerges" },
  { matter_id: "3", matter_number: "190019", description: "Administration of Estate - Joanne Trebell", client_name: "Joanne Rosemary Trebell" },
  { matter_id: "4", matter_number: "220014", description: "Power of Attorney - Julie Fairfield", client_name: "Julie Kay Fairfield" },
];

export const MOCK_RESULT: SearchResult = {
  status: "ok",
  query: "What's the status on the Smith estate?",
  matter: {
    name: "Estate of John Smith",
    type: "estate",
    refs: [
      { system: "estateably", id: "E1", label: "Estate of John Smith" },
      { system: "clio", id: "1382401266", label: "00042-Smith" },
    ],
  },
  answer: {
    parse_ok: true,
    insufficient_info: false,
    missing_sections: [],
    sections: {
      current_stage:
        "Early administration and file opening. The file is open in both Clio and Estateably with the standard folder structure active. (estateably:E1; clio:1382401266)",
      completed_items:
        "- Matter opened in Clio for trust accounting and time. (clio:1382401266)\n- Estate file opened in Estateably; Will and death certificate uploaded to the Permanent folder. (estateably:D1)\n- Interim distributions and releases prepared (estateably:E1; D1).\n- DocuSign envelope for tax filings completed (outlook:AAMkTEST123=, 2026-03-27).\n- Correspondence thread not surfaced (outlook:AAMkADlkZGQ1...AAExUjOuAAA).\n- Initial beneficiary list started.",
      outlook_correspondence:
        "Outlook is not available in this dossier (correspondence search is not yet enabled). Not reviewed.",
      outstanding_from_correspondence:
        "Not available — Outlook correspondence has not been searched for this matter.",
      kyc_intake:
        "KYC is not complete. Client Intake Forms are outstanding for the co-executors and beneficiaries receiving funds. No government ID or documented risk rating is on file in the KYC folder yet.",
      tax_status:
        "Tax filing status is not confirmed. No terminal T1 or T3 records are present. The terminal T1 deadline should be diarized (later of April 30 of the following year or 6 months after death). (clio:9)",
      assets_liabilities_banking:
        "Asset inventory is preliminary. Mandatory searches (LTO, PPSA, Bank of Canada, wealth) are outstanding. No trust banking activity recorded.",
      distribution_readiness:
        "Not ready for distribution. KYC, asset inventory, tax status, CRA clearance and beneficiary releases are all incomplete.",
      upcoming_deadlines:
        "1. Terminal T1 return.\n2. Wills variation limitation (210 days from probate grant).\n3. Initial T3 year-end.\n4. Executor's first accounting (within one year of grant).",
      risks_blockers:
        "KYC incomplete; tax filing status unconfirmed; mandatory searches outstanding. Do not distribute until beneficiary identity, tax status and releases are complete.",
      sources_reviewed:
        "Estateably estate summary and folders (Permanent, KYC, Tax); Clio matter and file-opening notes.",
      suggested_next_action:
        "Complete KYC intake for all co-executors and beneficiaries, diarize the terminal T1 deadline, and confirm asset security and mandatory searches.",
    },
    citations: [
      { system: "estateably", record_id: "E1", note: "estate summary — stage", url: "https://app.estateably.com/#/estates/E1/overview" },
      { system: "estateably", record_id: "D1", note: "Will.pdf in Permanent", url: "https://app.estateably.com/#/estates/E1/overview" },
      { system: "clio", record_id: "1382401266", note: "matter / file-opening notes", url: "https://app.clio.com/nc/#/matters/1382401266" },
      { system: "clio", record_id: "9", note: "terminal T1 task", url: "https://app.clio.com/nc/#/matters/1382401266" },
      { system: "outlook", record_id: "AAMkTEST123=", note: "DocuSign completion notification, 2026-03-27", url: "https://outlook.office365.com/owa/?ItemID=AAMkTEST123%3D&exvsurl=1&viewmodel=ReadMessageItem" },
    ],
    invalid_citations: [],
    sources_reviewed: [
      "Estateably — Estate of John Smith — summary, KYC & Tax folders",
      "Clio — matter 00042-Smith — notes & tasks",
    ],
  },
  usage: { model: "us.anthropic.claude-sonnet-4-6", input_tokens: 2140, output_tokens: 1180 },
};

export const MOCK_REPORTS: ReportEntry[] = [
  {
    // Committeeship / POA matter: real property + GIC accounts, with review flags.
    matter_number: "220014",
    trust_name: "Power of Attorney - Julie Fairfield",
    period: "2026-Q2",
    period_start: "2026-04-01",
    period_end: "2026-06-30",
    generated_at: "2026-07-15T13:02:00Z",
    review_status: "pending_review",
    cash_balance: 41250.0,
    investment_pending: true,
    asset_breakdown: {
      Cash: 41250.0,
      "Real Property": 875000.0,
      Investments: 0,
      GICs: 120000.0,
    },
    unclassified_accounts: ["Estate Holdback 2231"],
    stale_valuations: [
      { account: "Assets Under Administration - Real Property", as_at: "2024-01-10" },
    ],
    real_property_as_at: "2024-01-10",
    files: {
      cover_letter: "quarterly/220014/2026-Q2/cover-letter.docx",
      trust_ledger: "quarterly/220014/2026-Q2/trust-ledger.pdf",
    },
  },
  {
    // A straightforward cash-only trust: draft ready, only the portfolio fill-in pending.
    matter_number: "260012",
    trust_name: "John Dalton Burgess Trust",
    period: "2026-Q2",
    period_start: "2026-04-01",
    period_end: "2026-06-30",
    generated_at: "2026-07-15T13:01:30Z",
    review_status: "pending_review",
    cash_balance: 8450.25,
    investment_pending: true,
    asset_breakdown: { Cash: 8450.25, "Real Property": 0, Investments: 0, GICs: 0 },
    files: {
      cover_letter: "quarterly/260012/2026-Q2/cover-letter.docx",
      trust_ledger: "quarterly/260012/2026-Q2/trust-ledger.pdf",
    },
  },
];

export const MOCK_AML_EXPORTS: AmlExportEntry[] = [
  {
    period: "2026-07",
    generated_at: "2026-07-01T00:00:00Z",
    review_status: "pending_review",
    parties_count: 214,
    excluded_count: 18,
    excluded_parties_count: 46,
    roster_incomplete_count: 49,
    exceptions_count: 63,
    workbook_key: "aml/2026-07/AML_Name_Export_2026-07-01.xlsx",
  },
  {
    period: "2026-06",
    generated_at: "2026-06-01T00:00:00Z",
    review_status: "pending_review",
    parties_count: 209,
    excluded_count: 18,
    excluded_parties_count: 44,
    roster_incomplete_count: 49,
    exceptions_count: 71,
    workbook_key: "aml/2026-06/AML_Name_Export_2026-06-01.xlsx",
  },
];

export const MOCK_SCREEN_RESULT: ScreenResult = {
  total_screened: 66,
  total_flagged_persons: 2,
  total_matches: 2,
  high_confidence_count: 1,
  potential_match_count: 1,
  clear_count: 64,
  file_hash: "9f2c1a7b4e8d3f60a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718",
  flagged_results: [
    {
      client_name: "Viktor Petrov",
      client_dob: "1970-03-20",
      matter_number: "250043",
      role: "Beneficiary 1",
      source_tab: "Estates",
      matched_name: "Viktor PETROV",
      full_name: "Viktor Petrov",
      score: 97,
      category: "High Confidence",
      category_color: "red",
      source_list: "Canada-SEMA",
      government_id: "SEMA-1-42",
      sanction_dob: "1970",
      dob_match: true,
      status: "pending",
    },
    {
      client_name: "Maria Gonzalez",
      client_dob: "1985-11-02",
      matter_number: "220014",
      role: "Client",
      source_tab: "POA",
      matched_name: "Maria Gonzales",
      full_name: "Maria Gonzales",
      score: 89,
      category: "Potential Match",
      category_color: "orange",
      source_list: "UN",
      government_id: "UN-6907993",
      sanction_dob: "Unknown",
      dob_match: false,
      status: "pending",
    },
  ],
};

export const MOCK_CERTIFICATES: CertificateEntry[] = [
  {
    generated_at: "2026-07-02T15:12:00Z",
    review_status: "cleared",
    total_screened: 64,
    initial_matches: 1,
    confirmed_count: 0,
    dismissed_count: 1,
    file_hash: "a1b2c3d4e5f60718",
    reviewed_by: "jane@aphrodite.example",
    certificate_key:
      "screener/2026-07/2026-07-02T15-12-00Z_a1b2c3d4/Certificate_of_Search_2026-07-02.pdf",
  },
];

// Integrations tab sample — three healthy connectors and one down (QuickBooks,
// expired token) with 2 consecutive failures so the pulsing alert + suggested-fix
// path render in mock mode. Newest-first downtime log.
export const MOCK_INTEGRATIONS_HEALTH: IntegrationsHealth = {
  alerting: true,
  integrations: [
    {
      integration: "clio",
      ok: true,
      checked_at: "2026-07-21T14:00:00Z",
      consecutive_failures: 0,
      detail: "ok",
    },
    {
      integration: "estateably",
      ok: true,
      checked_at: "2026-07-21T14:00:00Z",
      consecutive_failures: 0,
      detail: "ok",
    },
    {
      integration: "graph",
      ok: true,
      checked_at: "2026-07-21T14:00:00Z",
      consecutive_failures: 0,
      detail: "ok",
    },
    {
      integration: "quickbooks",
      ok: false,
      checked_at: "2026-07-21T14:00:00Z",
      consecutive_failures: 2,
      detail: "HttpError: QuickBooks token refresh failed: 400",
      suggested_fix:
        "The QuickBooks OAuth token has expired or been revoked. Re-authorize the connection — run the QuickBooks bootstrap to mint a fresh refresh token (it self-rotates thereafter). See the QuickBooks setup guide.",
    },
  ],
  log: [
    {
      integration: "quickbooks",
      ok: false,
      at: "2026-07-21T13:00:00Z",
      detail: "HttpError: QuickBooks token refresh failed: 400",
    },
    {
      integration: "estateably",
      ok: true,
      at: "2026-07-20T09:00:00Z",
      detail: "ok",
    },
    {
      integration: "estateably",
      ok: false,
      at: "2026-07-20T06:00:00Z",
      detail: "ReadTimeout: read timed out",
    },
  ],
};

export const MOCK_ANSWER: SearchResult = {
  status: "answer",
  matter: {
    name: "Estate of John Smith",
    type: "estate",
    refs: [
      { system: "estateably", id: "E1", label: "Estate of John Smith" },
      { system: "clio", id: "1382401266", label: "00042-Smith" },
    ],
  },
  quick: {
    intent: "balance",
    text:
      "Cash/estate balance (Estateably): **$41,250.00**. (estateably:E1)\n" +
      "Estate value: $312,400.00.\n" +
      "- Trust account: $41,250.00 (clio:1382401266)",
    citations: [
      { system: "estateably", record_id: "E1", note: "estate summary — balance", url: "https://app.estateably.com/#/estates/E1/overview" },
      { system: "clio", record_id: "1382401266", note: "Clio account — Trust", url: "https://app.clio.com/nc/#/matters/1382401266" },
    ],
  },
};

export const MOCK_DISAMBIGUATION: SearchResult = {
  status: "disambiguation",
  query: "status on Lee",
  candidates: [
    {
      name: "Estate of Margaret Lee",
      matter_type: "estate",
      score: 1.25,
      systems: ["clio", "estateably"],
      refs: [
        { system: "estateably", id: "E7", label: "Estate of Margaret Lee" },
        { system: "clio", id: "5567", label: "00088-Lee" },
      ],
    },
    {
      name: "Lee Family Trust",
      matter_type: "trust",
      score: 0.9,
      systems: ["estateably"],
      refs: [{ system: "estateably", id: "T3", label: "Lee Family Trust" }],
    },
  ],
};

// Activity Logs sample — enough rows (and a spread of actions/users/times) to
// exercise pagination in mock mode. Built from a fixed base time so screenshots
// stay stable. Newest first, matching what the live API returns.
const MOCK_ACTIVITY_TEMPLATES: Omit<ActivityEntry, "timestamp" | "id" | "actor_email">[] = [
  { action: "matter.search", summary: "Ran a full matter search on “Estate of John Smith”", target: "Estate of John Smith", sources: ["clio", "estateably", "graph", "quickbooks"] },
  { action: "download", summary: "Downloaded a Certificate of Search", target: "screener/2026-07/…/Certificate_of_Search.pdf" },
  { action: "screener.certificate", summary: "Issued a Certificate of Search", target: "screener/2026-07/…/Certificate_of_Search.pdf" },
  { action: "screener.screen", summary: "Screened 64 name(s) against the sanctions watchlists (1 potential match(es))", target: "aml-export-2026-07.xlsx" },
  { action: "matter.search", summary: "Looked up “Lee Family Trust” (quick answer)", target: "Lee Family Trust", sources: ["estateably"] },
  { action: "aml.run", summary: "Ran the AML export on demand" },
  { action: "report.generate", summary: "Generated a Q2 2026 quarterly report draft", target: "00042-Smith" },
  { action: "matter.search", summary: "Ran a full matter search on “Wilkes Family Trust”", target: "Wilkes Family Trust", sources: ["clio", "estateably", "graph"] },
  { action: "download", summary: "Downloaded AML export workbook aml/2026-06/…", target: "aml/2026-06/active-clients.xlsx" },
  { action: "aml.schedule", summary: "Set the AML export schedule to run monthly on day 1 at 02:00" },
  { action: "matter.search", summary: "Searched for “Nakamura” — no matching matter found", target: "Nakamura", status: "success" },
  { action: "screener.delete", summary: "Removed a Certificate of Search", target: "screener/2026-06/…/Certificate_of_Search.pdf" },
];
const MOCK_ACTIVITY_USERS = [
  "jane@aphrodite.example",
  "alex@aphrodite.example",
  "admin@aphrodite.example",
];
const MOCK_ACTIVITY_BASE = Date.parse("2026-07-13T21:40:00Z");

export const MOCK_ACTIVITY: ActivityEntry[] = Array.from({ length: 44 }, (_, i) => {
  const t = MOCK_ACTIVITY_TEMPLATES[i % MOCK_ACTIVITY_TEMPLATES.length];
  return {
    ...t,
    id: `mock-${i}`,
    actor_email: MOCK_ACTIVITY_USERS[i % MOCK_ACTIVITY_USERS.length],
    timestamp: new Date(MOCK_ACTIVITY_BASE - i * 37 * 60_000).toISOString(),
    status: t.status ?? "success",
  };
});
