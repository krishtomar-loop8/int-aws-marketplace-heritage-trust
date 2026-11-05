# Phase 1 — Configuration & Business-Logic Genericization: Design

## Intent / why

Aphrodite is becoming an AWS Marketplace product deployed into each buyer's own
AWS account (see `MARKETPLACE_READINESS_PLAN.md`, Phase 1). Today several
business rules are hardcoded for one specific deployment ("Aphrodite" as both
vendor and customer): the search agent's system prompt, the AML own-company
exclusion check, the AML internal-matter filters, and the quarterly-report
letterhead. A second customer cannot be onboarded without an engineer editing
Python. This phase makes those values configurable per deployment, without
redesigning the underlying business logic.

## Goals

- Every value identified below is overridable per deployment via an
  `APHRODITE_*` environment variable, following the existing convention
  already used throughout the codebase (see `backend/api/handlers.py`,
  `backend/lambdas/*/handler.py`).
- Every new default equals Aphrodite's current value (the "seed with current
  values" decision), so existing behavior is unchanged until a deployment
  overrides it.
- Existing tests keep passing unmodified; new tests cover the override path
  and the explicit non-default behavior it produces.
- Two incidental cleanups found while reading these files are folded in:
  a leftover literal `"c/o Heritage Trust (mailing address)"` string in
  `framework.py` (survived the earlier rename because it wraps across two
  source lines), and bare first-name ("Nicole") decision-attribution comments
  in `quality.py` / `export.py`.

## Non-goals

- Redesigning the AML role taxonomy, the non-principal keyword list, or the
  screening logic itself — these read as generic trust/estate domain
  knowledge, not Aphrodite-specific, and stay as in-code defaults.
- A setup UI or admin screen for entering these values (Phase 3).
- Any change to connector (Clio/Estateably/QuickBooks/Graph) behavior.

## Anti-goals

- A config value whose override silently produces wrong output (e.g., an
  empty own-company list that's supposed to mean "exclude nothing" must
  behave that way, not throw, and must not accidentally match everything).
- Breaking Bedrock prompt-cache behavior: `render_framework()`'s output must
  still be byte-identical across every call **within one deployment's
  Lambda lifetime** (every call in one deployment uses the same env vars).
  It no longer needs to be byte-identical **across different deployments** —
  that was never the actual requirement.

## Configuration schema

| # | Value | Current hardcoded location | Env var | Default (seeded) |
|---|---|---|---|---|
| 1 | Org display name | `framework.py` (`FRAMEWORK` prompt) | `APHRODITE_ORG_NAME` | `"Aphrodite"` |
| 2 | Org office address (for the "c/o" mailing-address rule) | `framework.py` line 98-100 | `APHRODITE_ORG_OFFICE_ADDRESS` | `"545 Clyde Avenue, West Vancouver"` |
| 3 | HR/Finance excluded SharePoint folder names | `scoping.py::DEFAULT_EXCLUDED_FOLDERS` | `APHRODITE_EXCLUDED_SHAREPOINT_FOLDERS` (JSON list) | `["hr", "human resources", "finance"]` |
| 4 | Own-company name(s), excluded from AML screening | `quality.py::is_excluded_party_name` (hardcoded `"aphrodite"` prefix match) | `APHRODITE_OWN_COMPANY_NAMES` (JSON list) | `["Aphrodite"]` |
| 5 | Internal/administrative matter title keywords | `quality.py::INTERNAL_TITLE_KEYWORDS` | `APHRODITE_INTERNAL_TITLE_KEYWORDS` (JSON list) | current tuple verbatim |
| 6 | Internal matter-number pattern | `quality.py::_INTERNAL_NUMBER_RE` | `APHRODITE_INTERNAL_MATTER_NUMBER_PATTERN` (regex string) | `"^\\s*(2100-\\d+|999999)\\b"` |
| 7 | Letterhead org name | `render_docx.py::_LETTERHEAD_NAME` | `APHRODITE_LETTERHEAD_NAME` | `"Aphrodite"` |
| 8 | Letterhead address line | `render_docx.py::_LETTERHEAD_ADDRESS` | `APHRODITE_LETTERHEAD_ADDRESS` | current string verbatim |
| 9 | Letterhead signoff company | `render_docx.py::_SIGNOFF_COMPANY` | `APHRODITE_LETTERHEAD_SIGNOFF` | `"APHRODITE"` |
| 10 | Privacy-policy notice line | `render_docx.py::_PRIVACY_NOTICE` | `APHRODITE_LETTERHEAD_PRIVACY_NOTICE` | current string verbatim |

## Threading design (and why it differs by file)

Two different mechanisms are used, chosen per how deep the call chain is
between the Lambda handler (where env vars are read) and the function that
needs the value:

**Shallow call chains → explicit parameter threading** (used for #1, #2, #3,
#7-#10). The handler reads env vars once and passes them through 1-2
constructor/function calls — e.g. `handlers.py` → `MatterSynthesizer.__init__`
→ `render_framework(org_name=..., office_address=...)`. This matches the
existing pattern already used for `qualifying_practice_areas` and
`exclusion_field` in `quarterly_report/handler.py`. No global state, fully
explicit, easiest to test.

**Deep call chain → module-level config object** (used for #4, #5, #6).
`quality.is_excluded_party_name()` and `quality.exclusion_reason()` are
called from deep inside `export.py`'s internal party-building call graph
(`run_export` → several nested builder functions → `quality.X(...)`).
Threading three new parameters through every intermediate function in that
graph is a large, invasive change for a config-exposure task. Instead,
`quality.py` gets a module-level `OrgConfig` dataclass with a `configure()` /
`reset_config()` pair; `exclusion_reason` and `is_excluded_party_name` read
from the current config instead of the hardcoded module constants. The AML
export handler calls `quality.configure(...)` once at cold start, before
`run_export` is invoked — `export.py` itself needs no changes at all.
Tests reset the config in a fixture to avoid cross-test leakage.

## Out of scope for this phase (explicitly)

- `SCREENING_ROLES`, `_ROLE_SYNONYMS`, `_NON_PRINCIPAL_KEYWORDS` in
  `quality.py` — generic domain taxonomy, left as in-code defaults.
- Config-schema compatibility across version upgrades (Marketplace plan
  Phase 4, task 15).
