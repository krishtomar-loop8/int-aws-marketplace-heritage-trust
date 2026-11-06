# Connector Onboarding & Reconnect — Design

**Status:** builder-checked; open questions resolved below, pending final human approval.

## Decisions from builder-check

A fresh builder-check review raised three questions where a wrong guess
would have materially changed the plan. Resolved:

1. **Stale warm-container secrets on Estateably/Graph reconnect.** Clio and
   QuickBooks already re-read the secret (`refresh=True`) and retry once on
   an auth failure (built for Intuit's token-rotation races, but it happens
   to self-heal a reconnect too). Estateably and Graph don't — a Lambda
   container warm before a reconnect would keep minting credentials from
   the stale cached secret indefinitely. **Decision: add the same
   refresh-on-failure retry to the Estateably and Graph connectors**, so
   all four connectors self-heal a reconnect the same way, closing the gap
   rather than accepting a staleness window.
2. **Who can call the new `/connectors/*` write routes.** There's no
   role/group distinction anywhere in the app today — "behind Cognito"
   currently just means "logged in." **Decision: add a distinguished admin
   role.** This adds real scope beyond "join the existing route list": a
   Cognito user-pool group, a role/group check on these specific routes
   (the existing JWT authorizer validates audience/issuer only, not group
   membership, so the check happens in the route handler or a second
   authorizer), and frontend conditional rendering to hide the write
   controls from non-admin staff. Credential writes are more sensitive than
   the read-only routes everything else in the app exposes.
3. **Alert timing.** **Decision: fire the SNS alert at `consecutive_failures
   >= ALERT_THRESHOLD` (2), not on the raw down-flip**, matching the
   existing UI/`suggest_fix()` philosophy that a single blip is usually
   transient. This is a real implementation fork, not just a parameter: the
   first failure after a success IS the down-flip `_log_transition` already
   fires on (at count 1), but the threshold is crossed at count 2 *without*
   another flip — so the alert needs its own check inside
   `record_result` (`consecutive_failures == ALERT_THRESHOLD`, fired once
   per outage, not re-sent every subsequent hourly failure), not just a
   hook on `_log_transition`'s existing call site.

## Intent and why

Today, connecting a new customer's Clio, QuickBooks, Estateably, or Microsoft
Graph/SharePoint source to Aphrodite requires a Loop8 engineer to run
`scripts/clio_oauth_bootstrap.py` / `quickbooks_oauth_bootstrap.py` by hand,
with the customer's credentials in front of them, and to hand-write a
Secrets Manager entry for Estateably/Graph. That doesn't scale past a
handful of customers and isn't something an AWS Marketplace buyer expects —
they expect to self-serve a working connection after deploying the stack.

This document designs the fix: an in-app "Setup" experience, inside the
Setup screen that lets a customer admin connect each source themselves, see
live status, and reconnect one that's broken — without a Loop8 engineer in
the loop for the Aphrodite-side steps. It replaces the open item from
`MARKETPLACE_READINESS_PLAN.md` Phase 3 ("either a web page or a CLI
wizard, TBD") with a concrete design, and folds in two gaps a review of
that phase surfaced: Estateably's non-OAuth auth has no onboarding task at
all, and there's no path for *reconnecting* a connector that breaks after
initial setup (only "connect once").

## What already exists (don't rebuild this)

- **`backend/api/integrations.py` — `IntegrationHealthStore`.** A complete
  health-monitoring backend already exists: per-connector current status,
  `consecutive_failures`, an append-only downtime log written only on
  up/down transitions, an `ALERT_THRESHOLD = 2`, and `suggest_fix()`, which
  turns an error detail into a plain-language fix suggestion. An hourly
  EventBridge-scheduled probe (`terraform/modules/search_api/main.tf`,
  "hourly connector health probe") already calls this.
- **`frontend/src/components/IntegrationsPanel.tsx` — the Integrations
  tab.** Already renders this data live via `GET /integrations`
  (`useIntegrationsHealth`). Explicitly documented as **read-only; nothing
  here changes a connector** — that's exactly the gap this design closes.
- **Per-connector OAuth logic already factored out of the CLI scripts**,
  contrary to this plan's assumption that it still needs doing:
  - `backend/connectors/clio/oauth_bootstrap.py` — `build_authorize_url()`,
    `exchange_code()`.
  - `backend/connectors/quickbooks/oauth_bootstrap.py` — same two
    functions, Intuit-specific.
  - `scripts/clio_oauth_bootstrap.py` / `quickbooks_oauth_bootstrap.py` are
    already thin CLI wrappers around these. The reusable function Phase 3
    tasks 3–4 ask for is already there; what's missing is a Lambda/route
    calling it instead of a human running the CLI.
- **`services/secrets.py` — `SecretsClient.put(name, secret)`.** Already
  the exact primitive for writing a connector's credentials to Secrets
  Manager and refreshing the in-process cache.
- **`backend/harness.py` — `run_checks()`.** The D2 connectivity harness
  the health probe uses; reusable for an immediate "did that connection
  actually work" check right after a save, not just on the next hourly
  tick.

## A finding that changes the plan's assumed scope

Phase 3 task 2 assumed Clio and QuickBooks both need a **hosted OAuth
redirect callback** (an API Gateway route). Reading the actual connector
code shows that's only true for one of them:

| Connector | Auth model | Needs a hosted redirect? |
|---|---|---|
| **QuickBooks** | Real OAuth redirect — Intuit has no out-of-band flow (`oauth_bootstrap.py` docstring). After consent, the browser lands on a URL we control with `?code=&realmId=&state=`. | **Yes** — this is the only connector that needs new, unauthenticated API Gateway infrastructure. |
| **Clio** | Out-of-band: the authorize URL's `redirect_uri` is `https://app.clio.com/oauth/approval` — Clio shows the code on-screen, it never redirects to us. | **No.** The admin pastes the on-screen code into a field; a normal authenticated API call does the exchange. |
| **Estateably** | Service-account `username`/`password` → JWT, minted lazily by the connector. No OAuth at all. | No — a plain credentials form. |
| **Graph/SharePoint** | Entra app registration, `client_id`/`client_secret`/`tenant_id`, client-credentials grant (app-only, admin-consented in Entra — `backend/connectors/graph/connector.py`). | No — a plain credentials form, after a manual, undocumented-by-us Entra consent step (already correctly flagged in the plan as a human step we can't automate). |

So of four connectors, only **one** needs new unauthenticated
infrastructure. The other three are variations on "authenticated form →
validate live → `SecretsClient.put`" using code that already exists.

## Goals

- A customer admin can connect, and later reconnect, all four source
  systems from inside the app, without a Loop8 engineer touching AWS
  credentials or running a script on their behalf.
- Every connect/reconnect attempt is validated live before it's reported
  as success (reuse `run_checks()` / the same harness the hourly probe
  uses) — no "looks connected" that turns out broken on first real search.
- A connector that goes unhealthy after initial setup is both visible in
  the app (already true today) and **actively surfaced** to the admin via
  email, not just discoverable if they happen to open the Integrations tab.
- Reuse the existing health-store, downtime log, and OAuth-exchange
  functions as-is; this design adds the missing "write" side, not a
  parallel system.

## Non-goals

- **Automating the external, vendor-console steps.** Creating a Clio
  Private App, a QuickBooks app, or an Entra app registration + admin
  consent still happens in that vendor's own console, by a human. This
  design starts at "the admin has credentials/a code in hand," per the
  plan's own framing ("assisted self-service," not zero-touch).
- **The Phase 1 business-config fields** (org name, letterhead, AML
  exclusion lists, etc.) are Terraform variables baked into Lambda
  environment variables at deploy time — they are a **separate** concern
  from connector credentials (which live in Secrets Manager and need no
  redeploy to change). Phase 3 task 8 ("a config step so an admin doesn't
  have to hand-edit Terraform") belongs with Phase 4's CloudFormation
  `Parameters` block at stack-create time, not with this screen. Left for
  Phase 4's design, not decided here.
- **Pricing/entitlement** (flat vs. usage-based) — a separate, business
  decision, tracked independently.

## Anti-goals (would be a failure even if "it works")

- A previously-stored secret's value is ever readable back through the API
  (write-only: the UI can show "Connected" + last-validated time, never the
  client_secret/password itself).
- Any admin/API log line containing a client_secret, password, auth code,
  or token — same discipline the existing bootstrap scripts already follow
  ("Credentials never pass through the command line or this program's
  output").
- Silent retry or silent fallback to stale credentials on a failed
  exchange — a failure must surface as a clear error on the card, not a
  swallowed exception.

## Design

### One screen, per-connector cards, four interaction shapes

Extend `IntegrationsPanel.tsx` from read-only status into an actionable
screen: each existing status row becomes a card with the health info it
already shows, plus an action area whose shape depends on the connector's
auth model (table above):

- **QuickBooks card:** a single "Connect" button. First connect: admin
  pastes the QuickBooks app's `client_id`/`client_secret` once (these get
  stored immediately, before the redirect, since the callback that
  completes the flow has no way to ask the admin for them again — see
  below); clicking "Connect" redirects the browser to Intuit. On return,
  the card shows validated status. "Reconnect" re-runs the same flow.
- **Clio card:** "Generate authorization link" → shows the link to send to
  (or have) a Clio user with full access click through; a "Paste the code
  Clio showed you" field + submit, which calls `exchange_code()` and
  stores the result. Same control serves reconnect.
- **Estateably card:** a form (`username`, `password`, optionally
  `base_url` if non-default) → "Save & validate" calls a live login
  attempt via the existing connector, then stores on success.
- **Graph/SharePoint card:** a form (`tenant_id`, `client_id`,
  `client_secret`) with a link to the written guide for the Entra
  app-registration + admin-consent steps above it → "Save & validate"
  performs a live client-credentials token request, then stores on
  success.

Every "Save & validate" / OAuth-complete path, on success, immediately
calls the connectivity harness for that one connector (not just waits for
the next hourly probe) and writes the result through the existing
`IntegrationHealthStore`, so the card's status updates without waiting up
to an hour.

### Backend routes

All new routes join the existing single HTTP API
(`aws_apigatewayv2_api.search` in `terraform/modules/search_api/main.tf`)
and the existing Lambda, **except one**:

- `POST /connectors/estateably`, `POST /connectors/graph`,
  `POST /connectors/clio/authorize-url`, `POST /connectors/clio/exchange`,
  `POST /connectors/quickbooks/start` — join the existing route list,
  behind the existing Cognito JWT authorizer, same as every other route
  today — **plus a new admin-group check** these routes alone require (see
  Decision 2 above): a Cognito user-pool group (e.g. `admins`), checked
  against the group claim on the validated JWT. The existing JWT authorizer
  validates audience/issuer only, not group membership, so this check lives
  in the route handler (or a second, group-aware authorizer), not in
  `aws_apigatewayv2_authorizer.cognito` itself. The frontend hides the
  Setup screen's write controls (Connect/Reconnect/credential forms) from
  non-admin staff, who still see the existing read-only status.
- `GET /connectors/quickbooks/callback` — **must** be reachable
  unauthenticated (Intuit's browser redirect carries no Cognito
  session/bearer token), so it needs its own
  `aws_apigatewayv2_route` with `authorization_type = "NONE"`, outside the
  current uniform `for_each` list. CSRF protection here is the `state`
  value minted by `/connectors/quickbooks/start` and checked on return —
  not Cognito. This is the one genuinely new piece of public-facing
  surface area this design introduces; it should get specific attention in
  Phase 5 (security hardening).

**Chicken-and-egg note for QuickBooks:** the callback route only receives
`code`/`realmId`/`state` from Intuit — it has no way to ask the admin for
`client_id`/`client_secret` again. So `/connectors/quickbooks/start` must
store those (without a refresh token yet) via `SecretsClient.put` *before*
redirecting, and the callback reads them back out of that same secret to
complete the exchange, then updates it with the refresh token + `realmId`.

### Reconnect & alerting

No new status model — `IntegrationHealthStore` already tracks exactly what's
needed (`ok`, `consecutive_failures`, transition log). Two additions:

1. Make the panel's existing status display actionable (above), so
   "Reconnect" is just the same control that did "Connect," surfaced
   whenever a card is down.
2. Add an **alert notification**: inside `record_result`, when
   `consecutive_failures` reaches `ALERT_THRESHOLD` (2) — not on the raw
   first-failure down-flip `_log_transition` already logs, and not re-sent
   on every subsequent hourly failure of the same outage — publish to a new
   SNS topic. A new Terraform variable, `integrations_alert_emails` (same
   pattern as the existing `quarterly_notify_emails`), subscribes the
   customer's staff so they learn a connector broke before a user hits a
   dead search.
3. **Harden Estateably and Graph against stale warm-container secrets**,
   matching the `refresh=True`-and-retry-once pattern
   `backend/connectors/clio/connector.py` and
   `backend/connectors/quickbooks/connector.py` already use on an auth
   failure. Without this, a Lambda container warm before a reconnect keeps
   using the old cached secret indefinitely (no cold start to pick up the
   change), so a "successful" reconnect wouldn't actually take effect on
   live search until that container recycles — contradicting this design's
   own goal that nothing "looks connected" while still broken.

## Relationship to `MARKETPLACE_READINESS_PLAN.md` Phase 3

This design replaces Phase 3's open "web page vs. CLI wizard" question
(**web page**, extending the existing Integrations tab) and its tasks 1–7
with the concrete shape above — tasks 3–4 ("refactor bootstrap logic into
a reusable function") are already done in the codebase and drop out
entirely. It adds an explicit Estateably onboarding task and a
reconnect/alerting task that weren't in the plan. Tasks 8–10 (business
config step, written guides, sandbox dry-run) are unaffected except that
task 8 is now explicitly scoped out of this screen (see Non-goals). The
plan document itself isn't rewritten here; that happens when this design
is turned into an implementation plan.

## What's left to the builder

- Exact React component file names/structure and exact request/response
  JSON shapes for the new routes (follow `backend/api/handlers.py`'s and
  `frontend/src/api.ts`'s existing conventions).
- Exact `state`-value storage/validation mechanism for the QuickBooks
  callback (e.g., encoded in the value itself vs. a short-lived DynamoDB
  record) — an implementation choice, not a design one.
- Whether the Clio/QuickBooks "paste client_id/client_secret" step stores
  them in the connector's final secret shape immediately or in a
  short-lived staging location — implementation detail, not behavior.
- The exact mechanism for the admin-group check (a group claim on the
  Cognito JWT checked in each route handler, vs. a second API Gateway
  Lambda authorizer scoped to the `/connectors/*` routes) and the group's
  name/provisioning (Terraform-created Cognito group vs. customer-managed).
- Test design (unit tests per route/component, TDD per the project's
  normal process) — not specified here.
