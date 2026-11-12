# Phase 3 completion: deployment config UI + customer setup guides — Design

## 1. Intent and why

`MARKETPLACE_READINESS_PLAN.md`'s Phase 3 ("Self-service onboarding experience") lists 10
tasks. Tasks 1–5 and 7 are already shipped — the connector-onboarding branch (merged to
`main` before Phase 2) built the OAuth callbacks, the admin-gated `/connectors/*` routes,
the Setup screen's connector cards, and the live health-check validation after connecting.

What's left:

- **Task 8** — a config step for the Phase 1 business-config fields (org name, letterhead,
  AML exclusion names, Clio practice areas, SharePoint sites), so an admin doesn't have to
  hand-edit Terraform.
- **Task 6** — written SharePoint/Graph admin-consent instructions, reachable from the
  Graph connector card (which already says "see the setup guide" — a dead reference today).
- **Task 9** — the customer-facing guides for the parts that can't be automated: creating a
  Clio Private App, a QuickBooks app, registering an Entra enterprise app + admin consent,
  and granting SharePoint `Sites.Selected`.
- **Task 10** (sandbox dry-run) is explicitly **out of scope** for this build — it needs a
  live AWS sandbox account and real vendor sandbox accounts, neither of which exists right
  now (the AWS account is closed). Left as an open follow-up.

Decided in conversation: Task 8 is a **UI form in the Setup screen**, not a
`deployment.tfvars.example` file, and changes **take effect immediately, with no
redeploy** — via a new runtime config store, with the existing Terraform variables/env
vars staying as the fallback/default for any deployment that never touches this screen.

## 2. Goals

- An admin can view and edit every Phase 1 business-config field from the Setup screen.
- A change takes effect immediately — including for a request already being served by a
  warm search_api container, not just "the next cold start" — no `terraform apply`.
- A deployment that never opens this screen behaves exactly as it does today — the
  Terraform-set env var is still read, nothing silently resets.
- The UI visibly distinguishes "using this deployment's default" from "explicitly
  configured here" per field — closing the "silent wrong default" risk Phase 1's own
  agenda named as the hard part of making these fields configurable.
- The Graph connector card's "setup guide" reference resolves to a real, written guide.
- Four setup guides exist and are reachable from the app.

## 3. Non-goals / anti-goals

- Not touching Terraform's ownership of these 13 fields — the variables stay exactly as
  they are; this adds a layer in front of them, it doesn't replace them.
- Not a general-purpose feature-flag/config system — scoped to exactly the 13 fields
  Phase 1 already enumerated and documented.
- Not rewriting `framework.py` / `quality.py` / `scoping.py` / `render_docx.py` /
  `classification.py` — recon confirmed none of them read `os.environ` directly; they
  already take these values as plain constructor/function parameters. Only each Lambda's
  own cold-start config resolution changes.
- Not building Task 10's sandbox dry-run.
- A setup guide is a plain, accurate markdown doc with exact console navigation steps —
  not a polished branded help center, and not illustrated with screenshots (no way to
  capture real ones without a live account to screenshot).

## 4. Current state (confirmed by recon)

- Every one of the 13 fields is read exactly once, at cold start, by one Lambda's own
  entrypoint (`backend/api/handlers.py::_build_deps`, `backend/lambdas/quarterly_report/
  handler.py`, `backend/lambdas/aml_export/handler.py`, the SharePoint crawler's handler),
  via `os.environ.get("APHRODITE_X", default)`, then passed as a plain parameter into the
  business-logic modules. Those modules have no other dependency on environment or config
  storage.
- `services/secrets.py::SecretsClient` is the existing precedent for a small, injectable,
  cached AWS client with `get()`/`put()` (used for the connector credentials). The new
  `ConfigClient` mirrors its shape, backed by SSM Parameter Store instead of Secrets
  Manager — this is non-secret configuration, and SSM is the right-sized store (no KMS
  key, no per-secret cost).
- The CloudFormation installer already has a working, Terraform-free SSM convention:
  `/aphrodite/installer/last-error` and `/aphrodite/installer/outputs/*` are written via
  `aws ssm put-parameter --overwrite` with no Terraform-managed `aws_ssm_parameter`
  resource — the parameter is created by its first write. The new config store follows the
  same shape under its own prefix.
- `_require_admin` (handlers.py) already gates the 5 `/connectors/*` routes — the new
  `/config` routes reuse it unchanged.
- The Graph `ConnectorCard` already has a hint referencing "the setup guide" with no link —
  confirmed dead; no `docs/guides/` (or equivalent) directory exists yet.
- Today, only 3 of the 13 fields (`org_name`, `org_office_address`,
  `excluded_sharepoint_folders`) are env vars on the search_api Lambda. The other 10 are
  env vars only on `quarterly_report`'s, `aml_export`'s, and the SharePoint crawler's own
  Lambda functions — and since `GET /config` is served by search_api (the only Lambda with
  an admin API), it can't read another Lambda's environment directly.

## Decisions from builder-check

- **Reporting every field's Terraform default from search_api (resolves the gap above):**
  all 13 fields become env vars on search_api's `aws_lambda_function.search` too (in
  addition to wherever else they already are), purely so `GET /config` can report an
  accurate `"default"` value for every field. No new cross-Lambda IAM (`lambda:
  GetFunctionConfiguration` or similar) — search_api just reads its own environment, same
  as it does for the 3 fields it already uses directly.
- **The shared resolution helper sits in front of each Lambda's existing list parser,
  unchanged, rather than consolidating them.** Recon found three near-duplicate ad hoc
  parsers today (`handlers.py`/the SharePoint crawler's `_excluded_folders_from_env`,
  `aml_export`'s `_json_list_from_env`, `quarterly_report`'s `_qualifying_from_env` — plus
  the crawler's stricter inline `json.loads` for `sharepoint_sites`) with slightly
  different fallback semantics. Consolidating them is a real cleanup but isn't something
  this project's own change creates an orphan of — per the repo's "touch only what the
  task requires" convention, each keeps its own function; the new helper just checks
  `ConfigClient` first and falls through to the existing (unmodified) parser when nothing's
  configured.
- **The Deployment Config tab is visible to everyone, not hidden from non-admins** — it
  follows the Integrations tab's existing precedent exactly (`isAdmin` gates the edit
  controls; a non-admin sees the same values read-only) rather than introducing a new
  "hidden tab" filtering concept that doesn't exist anywhere else in `nav.ts`/`App.tsx`
  today.
- **`PUT /config`'s validation failure is a flat `{"error": "<field>: <message>"}`**,
  matching `_response(400, {"error": str(exc)})`'s existing shape exactly (the same shape
  every other validation error in this API already uses) — not a new structured
  `{"errors": {"field": "message"}}` response, which would be the only endpoint in the API
  with that shape.

## 5. The field schema

One JSON blob, one SSM parameter, 13 fields (names match the existing Terraform variables
1:1 — no renaming):

| Field | Type | Consuming Lambda(s) | Terraform default |
|---|---|---|---|
| `org_name` | string | search_api | `"Aphrodite"` |
| `org_office_address` | string | search_api | `"545 Clyde Avenue, West Vancouver"` |
| `excluded_sharepoint_folders` | list[string] | search_api, sharepoint_crawler | `["hr","human resources","finance"]` |
| `own_company_names` | list[string] | aml_export | `["Aphrodite"]` |
| `internal_title_keywords` | list[string] | aml_export | 10 keywords (accounting, finance & compliance, …) |
| `internal_matter_number_pattern` | string (regex) | aml_export | `"^\s*(2100-\d+|999999)\b"` |
| `letterhead_name` | string | quarterly_report | `"Aphrodite"` |
| `letterhead_address` | string | quarterly_report | `"220-545 Clyde Avenue, West Vancouver, BC V7T 1C5 \| 778.742.5005 \| aphrodite.example"` |
| `letterhead_signoff` | string | quarterly_report | `"APHRODITE"` |
| `letterhead_privacy_notice` | string | quarterly_report | `"Our privacy practices are described at www.aphrodite.example/privacy."` |
| `quarterly_qualifying_practice_areas` | list[string] | quarterly_report | `[]` |
| `quarterly_exclusion_field` | string | quarterly_report | `"Report Exclusion"` |
| `sharepoint_sites` | list[string] | sharepoint_crawler | `[]` |

Each list field keeps the same validation its Terraform variable already enforces (no
blank entries for `excluded_sharepoint_folders`/`own_company_names`;
`internal_matter_number_pattern` must be non-empty and compile as a regex) — the UI can't
produce a worse-than-Terraform-default state.

## 6. API design

Both routes live in `backend/api/handlers.py` (the only Lambda with an admin-facing API),
admin-gated the same way the `/connectors/*` routes are:

- **`GET /config`** → `{"fields": {"<name>": {"value": <effective value>, "source":
  "configured" | "default"}}}` for all 13 fields. `"configured"` means this UI has written
  a value; `"default"` means it's reading straight from the Terraform-set env var (which
  may itself be a non-factory value the customer set at deploy time — "default" here means
  "not yet touched by this screen," not "factory default").
- **`PUT /config`**, body `{"<name>": <value> | null, ...}` — a partial update. `null`
  clears that key, reverting to the env var/default. Validated per-field before writing
  (same rules as the table above); a validation failure rejects the whole request with a
  field-level error, consistent with `ConnectorValidationError`'s existing shape.

## 7. Resolution order (every consumer)

`ConfigClient` value for that key (if present in the stored blob) → `os.environ.get(env_var,
default)` → the in-code default. One small shared helper implements this once.

**Decided in conversation: a saved change must take effect immediately, even for a request
already being served by a warm container** — not just at the next cold start. This only
bites for **search_api**, the one Lambda with a long-lived warm container serving many
concurrent requests via `_get_deps()`'s global `_DEPS` cache (confirmed:
`backend/api/handlers.py` builds `_DEPS` once and reuses it across every warm invocation).
The three scheduled Lambdas (quarterly_report, aml_export, the SharePoint crawler) don't
have this problem in practice — each is a batch run invoked at most a few times a day, with
no concurrent in-flight requests to keep stale; reading config once at the start of each
invocation (not cached across invocations) already satisfies "immediate" for them with no
further restructuring.

For search_api, this means the 3 fields it touches directly (`org_name`,
`org_office_address`, `excluded_sharepoint_folders`) can no longer be baked into `_DEPS`'s
cached objects once at cold start — the expensive objects stay cached (Bedrock client,
connector instances, DynamoDB clients), but these three values must be re-resolved from
`ConfigClient` on every request and threaded through as call-time parameters instead of
constructor state:

- `MatterSynthesizer` (`backend/search/synthesize.py`) currently takes `org_name`/
  `office_address` at construction, baked in once by `_build_deps()`. It needs to accept
  them on `.synthesize()` instead (or an equivalent per-call override), with the
  route/worker handler resolving fresh values from `ConfigClient` at the top of each
  request and passing them down — not rebuilding the whole synthesizer object.
- `DossierBuilder` (`backend/search/orchestrator.py`) currently takes
  `excluded_sharepoint_folders` at construction. Same treatment: resolved fresh per call and
  passed into `.build()` as a per-call argument, not baked into the cached instance.

`framework.py`/`quality.py`/`scoping.py`/`render_docx.py`/`classification.py` themselves
don't change at all — they still take plain parameters; only *where* those parameters come
from (call-time resolution vs. constructor-time) changes, and only in search_api.

## 8. IAM / Terraform changes

New IAM statements, following the exact `dynamic "statement"` conditional pattern Phase 2
just established:

- `ssm:GetParameter` on the single `/aphrodite/config/deployment` parameter ARN, granted to
  every Lambda role that reads config (search_api, quarterly_report, aml_export, the
  SharePoint crawler).
- `ssm:PutParameter` (and `DeleteParameter`, for the "clear back to default" path) on the
  same ARN, granted to search_api's role only — the one Lambda that writes.

No new Terraform-managed `aws_ssm_parameter` resource. The parameter is created lazily by
its first write (`put_parameter(..., Overwrite=True)` creates-or-updates), exactly like the
installer's own SSM parameters. A `ParameterNotFound` read means nothing has been
configured yet — every field reports `"source": "default"`.

## 9. Frontend

- A new **"Deployment Config"** tab (my call — a new entry in `nav.ts`, separate from
  "Integrations," since this is business configuration, not connector health/connect).
  Visible to everyone, same as Integrations — `isAdmin` gates the edit controls only (see
  "Decisions from builder-check" above), a non-admin sees every field read-only.
- One form section per consuming Lambda (Search, AML Export, Quarterly Reports,
  SharePoint), each field showing its current effective value, a "configured" vs "using
  default" badge, and a "Reset to default" action when configured (admin-only).
- Reuses the existing `ResultNotice`/banner conventions from connector onboarding for save
  feedback and validation errors.

## 10. Setup guides (Tasks 6 & 9)

- Four new markdown guides under `docs/guides/`: `clio-private-app.md`,
  `quickbooks-app.md`, `entra-admin-consent.md`, `sharepoint-sites-selected.md`.
- My call: shipped as static files in the web bucket alongside the SPA, served by the same
  CloudFront distribution (e.g. `/guides/entra-admin-consent.html`) — no new infrastructure,
  reachable at a stable URL from the first deploy.
- The Graph `ConnectorCard`'s hint links to `/guides/entra-admin-consent.html`; the new
  Deployment Config tab's SharePoint section links to
  `/guides/sharepoint-sites-selected.html` next to the `sharepoint_sites` field; the Clio
  and QuickBooks connector cards get the same kind of hint + link, pointing at their own
  guides (consistency with the Graph card, which is the only one with a hint today).
- Content: step-by-step prose with exact console navigation paths, no screenshots (noted
  limitation, not a blocker).

## 11. What's left to the builder

- Exact validation error message wording (follow the existing `ConnectorValidationError`
  tone).
- Visual layout details of the Deployment Config tab (follow the existing Setup screen's
  card/form patterns).
- Whether the SharePoint crawler Lambda's config read happens once per invocation or is
  cached across warm starts — follow whatever pattern `_build_deps`'s cold-start caching
  already establishes for that Lambda, if any.
