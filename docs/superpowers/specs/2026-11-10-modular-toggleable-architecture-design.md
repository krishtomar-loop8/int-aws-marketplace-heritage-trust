# Modular, Toggleable Architecture — Design

**Status:** builder-checked; open questions resolved below, pending final human approval.

## Decisions from builder-check

A fresh builder-check review raised four questions. Resolved:

1. **Disabling a connector pauses it; it does not destroy the stored
   credential.** The Secrets Manager secret resource itself stays
   unconditional — only the `search_api` Lambda's IAM grant to read/write
   it, and the connect/reconnect routes for it, become conditional on the
   flag. Re-enabling a connector later resumes instantly without the admin
   reconnecting from scratch. (Secrets Manager storage cost is trivial
   regardless; this is a UX decision, not a cost one.)
2. **Quarterly Reports' and AML Export's *list/download* routes must also
   be gated by the module flag, not just hidden in the nav.** The
   builder-check found that `deps.reports`/`deps.aml_exports` (the
   already-generated-output list/download routes) are keyed only off
   `outputs_bucket`, which is unconditional — so disabling `quarterly_enabled`
   would leave `GET /reports`/`/reports/download` reachable and working,
   enforced only by the frontend hiding the tab. That's exactly the
   one-directional-enforcement failure this doc's own Anti-goals section
   already rules out, just in the backend-routes direction rather than the
   Terraform-vs-frontend direction originally named. **Decision:** these
   routes also check the module's enabled flag (see Design below).
3. **`/capabilities`'s `connectors` map needs a new `Deps` field — there's
   no per-connector signal on `Deps` today.** Unlike
   `reports`/`aml_exports`/`screener`, the four raw connectors are
   constructed into `resolver`/`dossier_builder`/`quick` internally and
   never surfaced on `Deps` itself. **Decision:** add
   `Deps.connectors_enabled: frozenset[str]`, populated once in
   `_build_deps`, reused by `/capabilities`, the orchestrator's
   `disabled_sources`, and anywhere else that needs to know which
   connectors this deployment has — one source of truth, per this doc's
   own stated principle for the module flags.
4. **Correction, not a decision:** this doc's original "Current state"
   section claimed `DossierBuilder`/`MatterResolver`/`QuickAnswerService`
   "do not yet tolerate an absent connector." Re-checked: they already do,
   at the parameter level — every connector param already defaults to
   `None` and every call site already guards on it. The actual remaining
   gap is narrower than first stated; see the corrected Current State and
   Design sections below.

## Intent and why

Not every buyer uses Estateably; not every trust-law practice wants the
AML Export or Sanction Screener modules. Today the Terraform root always
wires up all four connectors (Clio, Estateably, QuickBooks, Graph) and
always deploys every module's resources — a buyer who wants only Matter
Search still pays for and operates infrastructure for modules they'll
never touch. This design (Phase 2 of `MARKETPLACE_READINESS_PLAN.md`)
makes each connector and each ancillary module independently
enable/disable via Terraform variables, with the invalid combinations
(a module enabled without the connector it needs) rejected at `terraform
plan` time rather than discovered as a runtime crash or a silent gap.

## Decision from brainstorming (binds this design)

When a module's required connector is disabled, **Terraform must refuse
to enable that module** (a `validation` block, checked at plan time) —
not have the module degrade gracefully at runtime. Graceful runtime
degradation is reserved for the one module actually designed to work with
any subset of connectors: Matter Search.

This resolves what the plan's own text left ambiguous — Clio feeds Matter
Search, Quarterly Reports, *and* AML Export; Estateably feeds Matter
Search *and* AML Export, but the plan's "graceful degradation" tasks only
discuss the Matter Search orchestrator. The other two connector-dependent
modules get a hard dependency instead.

## Current state (don't rebuild what already exists)

- **No module-level enable flag exists for Quarterly Reports, AML Export,
  or Sanction Screener.** The existing `quarterly_schedule_enabled` /
  `aml_export_schedule_enabled` / `sanctions_refresh_schedule_enabled`
  variables (`terraform/variables.tf`) only gate each module's EventBridge
  *trigger* — the Lambda, IAM role, and other resources are created
  unconditionally today, schedule on or off.
- **No connector-level enable flag exists at all.** `terraform/main.tf`'s
  `module "search_api"` block always passes all four connector secret
  ARNs; `backend/api/handlers.py::_build_deps` always constructs all four
  connectors unconditionally.
- **`DossierBuilder`/`MatterResolver`/`QuickAnswerService` already tolerate
  both a *failing* and an *absent* connector at the parameter level.**
  `DossierBuilder.__init__` (`orchestrator.py`), `MatterResolver.__init__`
  (`resolve.py`), and `QuickAnswerService.__init__` (`quick_answers.py`)
  already default every connector param to `None`, and every call site
  already guards on it (`if self._estateably and ...`, etc.) — a failing
  pull is recorded in `errors` and skipped, and an absent one is already
  skipped without an `errors` entry at all. **The actual gap is narrower
  than "make them tolerant": it's that nothing yet distinguishes, in the
  dossier itself, "this connector is absent because this deployment
  doesn't use it" from "this candidate just has no reference in that
  system" — today's per-candidate `"estateably" in ref_ids` style check
  conflates both. That distinction (a new `disabled_sources` field) is
  the one piece of real new work here.**
- **The backend already has the "absent ⇒ 503" pattern for the other
  three modules**, just not wired to an explicit flag:
  `backend/api/handlers.py`'s `/reports`, `/aml-exports`, and
  `/screener/*` routes all check `if deps.X is None: return
  _response(503, {"error": "... not configured"})`. `deps.reports` /
  `deps.aml_exports` / `deps.screener` already end up `None` today purely
  as a side effect of `outputs_bucket` being unset — Phase 2 makes this an
  intentional, explicit toggle instead of an accident of other config.
- **The frontend nav has no hiding mechanism at all.** `frontend/src/nav.tsx`
  lists all 6 tabs with every one hardcoded `ready: true`; `frontend/src/config.ts`'s
  `AppConfig` has no notion of which modules are enabled.
- **Sanction Screener is not a separate Lambda/module** — unlike Quarterly
  Reports and AML Export (each `module "quarterly_report"` /
  `module "aml_export"` with their own dedicated Lambda), the screener's
  routes (`/screener/screen`, `/screener/certificate`, etc.) live inside
  the **same** `search_api` Lambda that serves Matter Search. "Disabling"
  it can't mean "don't deploy a Lambda" — see Design below.
- **`reference_bucket`** is shared between Quarterly Reports (the
  account-classification override workbook) and Sanction Screener (the
  watchlist bundle) — it is not screener-specific and must not be made
  conditional on `sanction_screener_enabled`.
- **`backend/api/handlers.py::_build_deps`** also constructs
  `MattersService(clio)` and `QuarterlyTrigger`/`AmlExportTrigger`
  unconditionally from the shared `clio` connector instance (not from the
  `quarterly_report`/`aml_export` Lambda modules themselves) — these feed
  the portal's **on-demand** "run now" triggers and need the same
  None-tolerance as the dedicated modules.

## Goals

- A buyer can enable/disable each of the four connectors and three
  ancillary modules (Quarterly Reports, AML Export, Sanction Screener)
  independently via Terraform variables, with only the resources each
  needs actually created.
- Enabling a module whose connector dependency is off is rejected at
  `terraform plan`/`validate` time with a clear error — never a runtime
  surprise.
- Matter Search degrades gracefully to whichever connectors are enabled:
  no crash, and the synthesized answer distinguishes "this source wasn't
  queried because this deployment doesn't use it" from "this source
  failed and the gap may be real."
- The frontend hides a disabled module's nav tab entirely, reading a
  capabilities response from the API (not a hardcoded build-time file).
- The Integrations health tab only lists enabled connectors.

## Non-goals

- **Matter Search itself is not toggleable.** It's the core product;
  every deployment has it. Only *which connectors feed it* varies. (The
  plan's own task list names a module-level flag only for Quarterly
  Reports/AML Export/Screener — not Matter Search.)
- **No in-app toggle UI.** Enabling/disabling happens via Terraform
  variables at deploy time, same as every other per-deployment config
  knob so far (Phase 1's org identity, this phase's flags). A future
  phase may add a setup-time UI for these; not this one.
- **No pricing/entitlement tie-in.** The plan's "ideally price what you
  use" framing is Phase 4's billing/entitlement decision, already
  tracked separately. This phase builds the on/off mechanism only.
- **Activity Logs and Integrations are not toggleable.** They're
  cross-cutting (chain-of-custody audit trail; connector health) rather
  than a buyer-facing module choice.

## Anti-goals (would be a failure even if "it works")

- A disabled module's Terraform resources still silently exist (cost
  without value) because a reference to them elsewhere wasn't made
  conditional too.
- Matter Search crashes, rather than degrades, when a connector is absent.
- The synthesized answer reports a disabled connector as a *failure*
  ("Estateably could not be reached") rather than a deployment choice
  ("Estateably is not used by this deployment") — the exact confusion the
  plan's own "difficult parts" section calls out.
- A module's dependency validation only exists in one direction (e.g.
  Terraform enforces it but the frontend capabilities response still
  claims AML Export is enabled when Clio isn't, because its own flag was
  checked instead of the real cross-dependency).

## Design

### Terraform: flags + conditional resources + dependency validation

Eight new root variables in `terraform/variables.tf`, each
`type = bool, default = true` for the four connectors (matches "every
deployment starts fully capable, opt out") and `default = false` for the
three ancillary modules (matches the existing security-conscious default
of `*_schedule_enabled = false`):

```hcl
variable "clio_enabled"       { type = bool, default = true }
variable "estateably_enabled" { type = bool, default = true }
variable "quickbooks_enabled" { type = bool, default = true }
variable "graph_enabled"      { type = bool, default = true }

variable "quarterly_enabled" {
  type    = bool
  default = false
  validation {
    condition     = !var.quarterly_enabled || var.clio_enabled
    error_message = "quarterly_enabled requires clio_enabled (Quarterly Reports reads Clio exclusively)."
  }
}
variable "aml_export_enabled" {
  type    = bool
  default = false
  validation {
    condition     = !var.aml_export_enabled || (var.clio_enabled && var.estateably_enabled)
    error_message = "aml_export_enabled requires both clio_enabled and estateably_enabled."
  }
}
variable "sanction_screener_enabled" { type = bool, default = false }
```

- `module "quarterly_report"` and `module "aml_export"` each get
  `count = var.quarterly_enabled ? 1 : 0` / `count = var.aml_export_enabled
  ? 1 : 0`. Every cross-module reference to them in `terraform/main.tf`
  (`module.quarterly_report.function_name`, `.function_arn`, etc., used by
  `search_api`'s `quarterly_function_name`/`quarterly_function_arn` and
  similarly for `aml_export`) becomes `try(module.quarterly_report[0].function_name,
  "")` (or an empty/null equivalent `search_api` already treats as
  "trigger not configured" — it does, via the existing `if quarterly_fn
  else None` pattern in `_build_deps`).
- `module "sanctions_refresh"` gets `count = var.sanction_screener_enabled
  ? 1 : 0` (the watchlist-bundle refresher is screener-specific and has
  no other consumer).
- The screener's *routes* (inside the always-deployed `search_api`
  Lambda) are gated by a new env var,
  `APHRODITE_SANCTION_SCREENER_ENABLED = var.sanction_screener_enabled`,
  read in `_build_deps` to decide whether `deps.screener` is constructed
  — replacing today's accidental "`None` only because `outputs_bucket`
  happens to be unset" with an explicit, intentional toggle. `reference_bucket`
  stays unconditional (shared with Quarterly Reports).
- **The secret resource itself stays unconditional** (Decision 1) — only
  the `search_api` IAM statements that grant access to it
  (`WriteXToken`/`ReadSecrets`) become conditional, wrapped in
  `dynamic`/`for_each` keyed off the flag, mirroring the connector-onboarding
  branch's per-secret IAM statement shape. Pausing a connector revokes the
  Lambda's ability to read/write it; re-enabling restores access to the
  still-intact credential. The connector-onboarding branch's `/connectors/*`
  routes (connect/reconnect) also check the flag — paused connectors can't
  be reconnected through the Setup screen either, consistent with "this
  deployment doesn't use this connector right now."

### Backend: an explicit connector-enabled signal + the one real gap

- `backend/api/handlers.py::_build_deps`: a connector's secret ARN is
  never passed to the Lambda as an env var at all (only used for IAM
  policy construction) — the right signal is instead four **new** env
  vars, one per connector (`APHRODITE_CLIO_ENABLED`, etc.), set directly
  from each Terraform `*_enabled` flag (Terraform's `true`/`false` → the
  string `"true"`/`"false"`). Construct each connector only when its flag
  reads `"true"`; `resolver`/`dossier_builder`/`quick`/`matters` already
  tolerate the `None` this produces (see the corrected Current State above
  — no change needed there). Add `Deps.connectors_enabled: frozenset[str]`,
  populated once from the same four checks, as the single source of truth
  `/capabilities` and the orchestrator both read (Decision 3).
- **No new env var is needed for the module-level flags** — `_build_deps`
  already computes `quarterly_fn = os.environ.get("APHRODITE_QUARTERLY_FUNCTION")`
  and `aml_export_fn` the same way, which are already empty-string (falsy)
  once `module.quarterly_report`/`module.aml_export` are `count = 0` and
  their outputs become conditional at the root (`var.quarterly_enabled ?
  module.quarterly_report[0].function_name : ""`). Reuse that same
  truthiness for `deps.reports`/`deps.aml_exports` (Decision 2) instead of
  introducing a parallel signal.
- `backend/search/orchestrator.py`: add `disabled_sources: frozenset[str]`
  to the dossier, populated from `connectors_enabled`'s complement, and
  have the per-candidate skip logic (today's `"estateably" in ref_ids`
  style checks) consult it so a connector absent *by design* is never
  conflated with a candidate that simply has no reference in that system
  — this is the one actual implementation gap (see corrected Current
  State).
- `backend/search/synthesize.py::MatterSynthesizer`: reads
  `disabled_sources` and phrases those sources in the prompt/output as
  "not used by this deployment," never alongside the genuine-failure
  phrasing the existing `errors` dict already produces.
- `backend/api/handlers.py`'s `/reports`, `/reports/download`,
  `/aml-exports`, `/aml-exports/download` routes (Decision 2): gate
  `deps.reports`/`deps.aml_exports` on the module's enabled flag too, not
  only on `outputs_bucket` — e.g. `reports = ReportsService(s3, outputs_bucket)
  if s3 and outputs_bucket and quarterly_enabled else None`, reading the
  same `APHRODITE_QUARTERLY_ENABLED`-style env var `deps.quarterly`
  already keys off. A disabled module's API becomes genuinely
  unreachable, not just hidden in the nav.

### A new `GET /capabilities` route

Returns `{"modules": {"quarterly_reports": bool, "aml_export": bool,
"sanction_screener": bool}, "connectors": {"clio": bool, "estateably":
bool, "quickbooks": bool, "graph": bool}}`. Modules come from the same
`deps.X is None` checks those routes themselves use (now module-flag-aware
per Decision 2); connectors come from `deps.connectors_enabled` (Decision
3) — one source of truth either way, no duplicated Terraform-output
plumbing into a separately-built `config.json` (today's `config.json` is
hand-assembled by the CI workflow's heredoc from `terraform output -raw`
values; adding a map output there would mean keeping two places in sync).
Matter Search and Activity Logs are never included (Non-goals) — the
frontend always shows those tabs.

### Frontend: nav hides a disabled module's tab

- `frontend/src/config.ts` gains a `capabilities` fetch (new function in
  `api.ts`, same `authHeaders`/`fetch` pattern as every other GET).
- `frontend/src/nav.tsx`'s `TABS` stays the static list; `App.tsx` filters
  it by the fetched capabilities before handing it to `Sidebar`, hiding
  `quarterly-reports`/`aml-export`/`sanction-screener` when their flag is
  false (`matter-search`, `activity-log`, `integrations` always show).
- The Integrations tab's `IntegrationsPanel` filters its connector rows to
  only the ones the same capabilities response marks enabled (mirroring
  `CONNECTABLE` filtering already added in the connector-onboarding
  branch).

## What's left to the builder

- Exact `dynamic`/`for_each` shape for the conditional IAM statements
  (follow the connector-onboarding branch's per-secret statement pattern).
- Exact `disabled_sources`/dossier field name and type if the
  implementer finds a closer fit once inside `orchestrator.py`'s existing
  dataclasses.
- `APHRODITE_SANCTION_SCREENER_ENABLED`'s exact read mechanism in
  `_build_deps` (string comparison, same as the four new connector flags).
- Whether `/capabilities` sits behind the existing Cognito JWT authorizer
  (consistent with every other route) or is left public (arguably fine
  since it reveals no data, only which modules exist) — default to
  behind the authorizer, consistent with everything else, unless the
  implementer finds a concrete reason not to.
- Test design (unit tests per route/component/Terraform validation,
  TDD per the project's normal process) — not specified here.
