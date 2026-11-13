# First-run setup wizard — Design

## 1. Intent and why

Today, a brand-new deployment's admin logs in (via Cognito SSO, federated through
the customer's own Entra ID/M365) and lands on **Matter Search** — empty, nothing
connected, no business config set — with no guidance toward the Integrations tab
(connector credentials) or the Deployment Config tab (business config). Both of
those already let an admin set everything up without a redeploy (Integrations
writes to Secrets Manager; Deployment Config writes to SSM Parameter Store), but
nothing in the product tells a first-time admin they exist.

A full-screen, step-by-step **setup wizard** closes this gap: it's the first
thing the admin sees on a fresh deployment, walks them through connecting each
enabled source and setting business config, and then gets out of the way for
good.

## 2. Goals

- A fresh deployment's first login (by the admin who deployed it) shows a
  dedicated, full-screen wizard — no sidebar, no other tab reachable — instead
  of the normal app shell.
- The wizard's steps are generated from what this specific deployment actually
  has enabled (Phase 2's modular toggles, read via the existing
  `GET /capabilities` route) — a deployment with QuickBooks disabled never
  shows a QuickBooks step.
- Every step (including each of the 4 connectors) has a **"Skip for now"** — the
  admin is never blocked from reaching the real app. A skipped connector is
  completed later via the existing Integrations tab; skipped business-config
  fields via the existing Deployment Config tab — both unchanged.
- The wizard includes a final step (or steps) covering the business-config
  fields already shipped in the Deployment Config tab.
- Once the admin reaches the wizard's end — by finishing or skipping
  everything — it's dismissed for good and never auto-shows again.
- A non-admin who logs in before setup is complete sees a simple, read-only
  message instead of the wizard (they can't complete it — connecting a source
  requires the `admins` Cognito group).

## 3. Non-goals / anti-goals

- Not a general-purpose onboarding/tutorial system — scoped to exactly the
  5-6 things a fresh deployment needs (4 connectors + business config).
- Not solving Graph/SharePoint's external Entra admin-consent step — that
  still happens outside this app entirely; the wizard's Graph step is just
  the same credential form the Integrations tab already has, with a
  "Skip for now" if the admin hasn't done the external step yet.
- Not building a parallel onboarding flow for non-admin users — they get a
  one-line "ask your administrator" message, not a cut-down wizard.
- Not re-architecting the existing Integrations or Deployment Config tabs —
  the wizard reuses their existing forms/components as its step content, it
  doesn't duplicate them.
- Never silently assumes a skipped step is done — "skipped" and "done" are
  visibly different states, both here and later in Integrations/Deployment
  Config.

## 4. Current state (relevant pieces already built)

- `GET /capabilities` (unauthenticated-safe, everyone can read it) returns
  `{"modules": {"quarterly_reports": bool, "aml_export": bool,
  "sanction_screener": bool}, "connectors": {"clio": bool, "estateably": bool,
  "quickbooks": bool, "graph": bool}}` — `backend/api/handlers.py:666-676`.
  `true` means this deployment has that connector/module enabled at the
  Terraform level (Phase 2), not that it's been connected yet.
- `GET /integrations` returns each connector's current health row
  (`{"integration": "clio", "ok": bool, "checked_at": ..., ...}`) — a row
  exists once a connector has ever been connected OR ever been probed by the
  hourly health check, whichever came first. A successful `/connectors/*`
  connect call already writes a health row immediately
  (`backend/api/connectors_admin.py`'s `_record_health` helper) — so "a row
  exists for this connector" is a reliable "has this ever been connected"
  signal, distinct from "is it currently healthy" (a once-connected connector
  that later goes down still has a row, and must not look "unconfigured"
  again to the wizard).
- `frontend/src/App.tsx` already fetches `Capabilities` on mount and derives
  `isAdmin` from the Cognito JWT's `cognito:groups` claim
  (`frontend/src/main.tsx:41-42`, real auth path only — mock mode has no
  wizard, see §9).
- `ConnectorCard.tsx` (4 connector forms) and `DeploymentConfigPanel.tsx` (13
  business-config fields) are the existing, working UI for everything this
  wizard needs to present — the wizard's step content reuses these components
  directly, it does not reimplement them.

## 5. The wizard's step list

Generated dynamically, not hardcoded, from `GET /capabilities`:

1. One step per `connectors.<name> === true` entry, in a fixed order
   (Clio, Estateably, QuickBooks, Graph) — reusing `ConnectorCard`'s existing
   form for that connector. A connector with `connectors.<name> === false`
   gets no step at all.
2. **One** final step for all 13 business-config fields (not four) — its
   content is internally organized into the same 4 visual sections the
   Deployment Config tab already uses (Search / AML Export / Quarterly
   Reports / SharePoint), reusing `DeploymentConfigPanel`'s existing field
   editors, but "Skip for now" applies to the whole step at once. (Settled
   during builder-check: §6 only ever defines one "already done" signal for
   business config — "at least one of the 13 fields configured" — which only
   makes sense for a single step; splitting into 4 steps would need 4
   separate signals this doc never defined.)

Each step shows: the connector/section's form, a **"Skip for now"** button, and
(once the admin has successfully submitted that step, or it's already done per
§6) a **"Continue"** button. The last step's "Continue" becomes **"Finish
setup"**.

## 6. Detecting "setup incomplete" and per-step "already done"

- **Per-connector step "already done":** a health row exists for that
  connector in `GET /integrations` (§4) — regardless of its current `ok`
  value. If the admin re-logs-in after connecting 2 of 4 and skipping the
  rest, the wizard (if it's still showing — see below) opens showing those 2
  as done, not re-asking.
- **Business-config step "already done":** at least one of the 13 fields
  reports `"source": "configured"` from `GET /config` — a light-touch
  signal, not "all 13 configured" (an admin who's set 3 of 13 fields has made
  real progress and shouldn't be treated as having skipped this step).
- **Whether the wizard shows at all, on a given login:** a single persisted
  "setup wizard dismissed" flag, set once when the admin reaches the end
  (finishes or skips through to "Finish setup"). The wizard shows only when
  this flag is unset. This is a deliberate, simple either/or — once
  dismissed, it never reappears automatically, even if the admin later
  disables and re-enables a connector; the admin always still has normal
  access to Integrations/Deployment Config to finish anything left undone.

## 6a. Resolved during builder-check

Three more decisions, all settled by applying this doc's own goals
consistently rather than left open — called out here so they're easy to
double-check:

- **The QuickBooks OAuth round trip, mid-wizard.** Connecting QuickBooks is
  the one step that isn't a same-page form submit: the browser fully leaves
  the app for Intuit and comes back via a cold page load to
  `{app_base_url}/?setup=quickbooks-connected|quickbooks-error` (the existing
  `quickbooks_callback` redirect). Today that query param forces the app into
  the Integrations tab so the existing `IntegrationsPanel` can show the
  connected/error banner. **My call:** that cold reload must still check the
  dismissed-flag like any other page load — if unset, it renders the wizard
  (not the shell), resumed at the QuickBooks step, which now shows "done" per
  §6's health-row signal, with the connected/error banner surfaced inside the
  wizard's QuickBooks step instead of `IntegrationsPanel`. Falling through to
  the normal shell here would mean the admin reaches "another tab" before
  finishing or explicitly skipping the rest — a direct contradiction of Goal
  2's "no other tab reachable."
- **Fail-open or fail-closed while the dismissed-flag is loading.** The
  existing `/capabilities` fetch in `App.tsx` fails *open* (shows everything
  until the real answer arrives), because hiding a tab that should be visible
  is the worse failure. The dismissed-flag is the opposite shape of risk: the
  worse failure here is letting an admin slip past setup into an empty,
  disconnected app on a slow/failed fetch. **My call:** fail *closed* — treat
  "not yet resolved" the same as "not dismissed" (show the wizard, or a brief
  loading state, never the real shell) until the fetch completes. The cost of
  being wrong is a returning admin occasionally seeing a flash of the wizard
  before it resolves to "already dismissed" — a one-time, self-correcting
  cost, versus letting a first-time admin through by accident.
- **Who can read the dismissed-flag.** Non-admins need to read it too (§7's
  fallback depends on it), so it follows the same visible-to-everyone,
  write-gated pattern `GET /capabilities` and `GET /config` already use — not
  the fully admin-gated pattern the `/connectors/*` write routes use.

## 7. Non-admin fallback

A non-admin who logs in while the dismissed-flag is unset sees a single
centered message in place of the normal app shell — something like "Your
administrator is still finishing setup. Check back soon, or ask them to sign
in and complete it." — with a sign-out action, nothing else. No partial
wizard, no connector forms (they can't submit them anyway — the `/connectors/*`
write routes are admin-gated, per existing Phase-3 work).

## 8. What's left to the builder

- **Where the "dismissed" flag lives.** My call: a new, small, dedicated
  mechanism — not a 14th entry crammed into `config_admin.py`'s existing
  13-field whitelist schema (that file's `put_config` already deliberately
  rejects any unknown key; overloading it would mix a UI-only system flag
  into a schema designed for customer-meaningful business config). A second,
  tiny SSM parameter (e.g. `/aphrodite/setup/dismissed`) read/written through
  a `ConfigClient`-like wrapper (or `ConfigClient` itself, parameterized) is
  the obvious shape, but the exact plumbing (a new tiny client vs. a second
  constructor argument on `ConfigClient` vs. something else) is an
  implementation decision, not a product one.
- Exact wizard visuals (progress indicator style, spacing, whether skipped
  steps show a distinct icon from done ones) — follow the existing
  Setup-screen/connector-card visual language, no new design system needed.
- Exact non-admin fallback copy.
- Whether "Finish setup" needs a confirming click or auto-dismisses the
  instant every step is either done or skipped — a small UX call, not a
  behavior this doc needs to pin down.

## 9. Mock mode

The wizard does not appear in `frontend/public/config.json`'s `mock: true`
local-dev mode — mock mode has no real `/capabilities`/`/integrations`/
`/config` backend to derive "incomplete" from, and every other panel already
treats mock mode as "skip real data, show fixtures." The wizard's existence is
exercised by a real deployment or a deliberate manual toggle in dev, not by
flipping `mock`.
