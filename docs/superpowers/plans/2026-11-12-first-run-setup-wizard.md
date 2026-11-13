# First-Run Setup Wizard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On a fresh deployment, the first admin login shows a full-screen,
step-by-step setup wizard (connect each enabled source, then set business
config) instead of the normal app shell — skippable at every step, dismissed
for good once reached, never shown again.

**Architecture:** A new, tiny "dismissed" flag reuses the existing
`ConfigClient` class (just a second instance pointed at a different SSM
parameter — no new backend class). Two new routes expose it. The frontend
gains a new top-level branch in `App.tsx`, ahead of the existing shell, that
renders the wizard or a read-only non-admin message instead — the wizard's
step content reuses the existing `ConnectorCard` and `DeploymentConfigPanel`
components unchanged.

**Tech Stack:** Python 3.13 (backend/Lambda), boto3 (SSM), TypeScript/React
(frontend), Terraform.

**Spec:** `docs/superpowers/specs/2026-11-12-first-run-setup-wizard-design.md`

## Global Constraints

- The wizard shows only to an admin (`isAdmin`); a non-admin sees a read-only
  "your administrator is still finishing setup" message instead (spec §7).
- The step list is generated dynamically from `GET /capabilities`'s
  `connectors` object, in fixed order Clio → Estateably → QuickBooks → Graph
  — a connector with `connectors.<name> === false` gets no step (spec §5).
- Business config is **one** step covering all 13 fields, not four (spec §5,
  resolved during builder-check).
- Per-connector "already done": a row exists for that connector in
  `GET /integrations`, regardless of its current `ok` value (spec §6).
- Business-config step "already done": at least one of the 13 fields reports
  `"source": "configured"` from `GET /config` (spec §6).
- The dismissed-flag is a single persisted boolean. `GET` is visible to
  everyone (no admin gate); only the dismiss action is admin-gated (spec §6a).
- The dismissed-flag fetch **fails closed**: while unresolved or on a fetch
  error, treat it as "not dismissed" — the opposite of this codebase's
  existing `fetchCapabilities` fail-open convention (spec §6a).
- The QuickBooks OAuth cold-reload (`?setup=quickbooks-connected|-error`)
  must resume inside the wizard (showing the connector step as done, with
  the banner) when the dismissed-flag is still unset — never fall through to
  the normal Integrations tab in that case (spec §6a).
- No new Terraform-managed `aws_ssm_parameter` resource — the dismissed-flag
  parameter is created lazily by its first write, exactly like
  `/aphrodite/config/deployment` (spec §8).
- The wizard never appears in `mock: true` local-dev mode (spec §9).

## Review Focus

- A deployment with all 4 connectors Terraform-disabled (Phase 2 allows
  this): the wizard's connector-step list is empty — it must jump straight
  to the business-config step, not render a zero-step wizard or crash.
- The dismissed-flag fetch failing (network error, non-200): must fail
  closed (show the wizard or the non-admin message) without throwing an
  unhandled error that blanks the whole app.
- The QuickBooks cold-reload landing with `?setup=quickbooks-error` while
  the wizard is still showing: the error banner must appear on the
  QuickBooks step, not silently disappear.
- Once dismissed, the flag must stay dismissed — a later Terraform change
  (disabling/re-enabling a connector) must never cause the wizard to
  reappear.
- A non-admin logging in after the admin has already dismissed setup must
  see the normal app shell, not the "still finishing setup" message —
  `isAdmin` alone must never gate this, only the dismissed-flag does.

---

### Task 1: Backend — `GET /setup-status` / `POST /setup-status/dismiss`

**Files:**
- Modify: `backend/api/handlers.py`
- Test: `backend/api/tests/test_handlers.py`

**Interfaces:**
- Consumes: `services/config.py::ConfigClient` (existing — already accepts a
  `parameter_name` override in its constructor, confirmed by reading the
  file; no new backend class needed).
- Produces: `Deps.setup_status: ConfigClient | None = None` (new field, last
  in the dataclass) — no later task in this plan consumes it directly, but
  it's the field Task 1's own routes read/write.

- [ ] **Step 1: Write the failing tests**

```python
def test_get_setup_status_route_not_dismissed_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.config import ConfigClient

    deps, _ = install_deps(monkeypatch)
    deps.setup_status = ConfigClient(client=_FakeSsmForConfig())
    resp = handlers.handler(api_event("GET", "/setup-status"))
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"]) == {"dismissed": False}


def test_dismiss_setup_status_route_requires_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.config import ConfigClient

    deps, _ = install_deps(monkeypatch)
    deps.setup_status = ConfigClient(client=_FakeSsmForConfig())
    resp = handlers.handler(api_event("POST", "/setup-status/dismiss"))
    assert resp["statusCode"] == 403


def test_dismiss_setup_status_route_success(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.config import ConfigClient

    deps, _ = install_deps(monkeypatch)
    deps.setup_status = ConfigClient(client=_FakeSsmForConfig())
    resp = handlers.handler(admin_api_event("POST", "/setup-status/dismiss"))
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"]) == {"dismissed": True}
    # Visible on a subsequent GET too (a fresh read, not just the POST's own echo).
    get_resp = handlers.handler(api_event("GET", "/setup-status"))
    assert json.loads(get_resp["body"]) == {"dismissed": True}
```

(`_FakeSsmForConfig` already exists in this test file from the
deployment-config plan — reuse it unchanged. Follow this file's existing
`install_deps`/`api_event`/`admin_api_event` conventions exactly.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest backend/api/tests/test_handlers.py -k "setup_status" -v`
Expected: FAIL — 404 (no `/setup-status` routes exist yet); `Deps` has no
`setup_status` field.

- [ ] **Step 3: Implement in `backend/api/handlers.py`**

Add `setup_status: ConfigClient | None = None` as the new last field of
`Deps`. In `_build_deps()`, construct it the same way `config` is already
constructed, but with the different parameter name:

```python
setup_status = ConfigClient(
    boto3.client("ssm", region_name=REGION),
    parameter_name="/aphrodite/setup/dismissed",
)
```

Pass `setup_status=setup_status` into the final `Deps(...)` call. Add two
routes, following the exact admin-gating style the `/config` routes already
use:

```python
if method == "GET" and path.endswith("/setup-status"):
    return _response(200, {"dismissed": bool(deps.setup_status.get().get("dismissed"))})

if method == "POST" and path.endswith("/setup-status/dismiss"):
    forbidden = _require_admin(event)
    if forbidden:
        return forbidden
    deps.setup_status.put({"dismissed": True})
    return _response(200, {"dismissed": True})
```

`GET /setup-status` is deliberately **not** admin-gated (Global Constraints —
everyone can read it; only the dismiss action is admin-only).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest backend/api/tests/test_handlers.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add backend/api/handlers.py backend/api/tests/test_handlers.py
git commit -m "feat: add GET/POST setup-status routes for the first-run wizard"
```

---

### Task 2: Terraform — routes + IAM for the dismissed-flag parameter

**Files:**
- Modify: `terraform/modules/search_api/main.tf`

**Interfaces:**
- Consumes: nothing from earlier tasks (pure Terraform; Task 1 already works
  against `boto3.client("ssm", region_name=REGION)` with no Terraform-specific
  assumption beyond the IAM grant existing).
- Produces: nothing later tasks consume.

- [ ] **Step 1: Add the two routes**

In `aws_apigatewayv2_route.routes`'s `for_each` list, add `"GET
/setup-status"` and `"POST /setup-status/dismiss"`, same `authorization_type
= "JWT"` as every other route in that resource (the admin check stays inside
the handler, exactly like `GET /config`/`PUT /config`).

- [ ] **Step 2: Add the IAM grants**

In `data.aws_iam_policy_document.search` (the same policy document the
deployment-config plan's `ReadDeploymentConfig`/`WriteDeploymentConfig`
statements are in), add two more statements, same shape, pointed at the new
parameter's ARN:

```hcl
statement {
  sid       = "ReadSetupStatus"
  actions   = ["ssm:GetParameter"]
  resources = ["arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/aphrodite/setup/dismissed"]
}
statement {
  sid       = "WriteSetupStatus"
  actions   = ["ssm:PutParameter"]
  resources = ["arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/aphrodite/setup/dismissed"]
}
```

Only search_api needs this grant — no other Lambda reads the dismissed-flag.

- [ ] **Step 3: Verify**

Run: `terraform -chdir=terraform fmt -check -recursive && terraform -chdir=terraform validate`
Expected: `Success! The configuration is valid` (same pre-existing warnings
only).

- [ ] **Step 4: Confirm the secrets module is untouched**

Run: `git status --porcelain` and confirm only
`terraform/modules/search_api/main.tf` is listed (same indirect-proof
pattern used in earlier phases, since `terraform/modules/secrets/` is
blocked from direct read/diff by this session's permission settings).

- [ ] **Step 5: Commit**

```bash
git add terraform/modules/search_api/main.tf
git commit -m "feat: wire setup-status routes and SSM IAM grants in Terraform"
```

---

### Task 3: Frontend — `fetchSetupStatus`/`dismissSetup` + mock fixture

**Files:**
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/mock.ts`

**Interfaces:**
- Consumes: Task 1's `GET /setup-status` / `POST /setup-status/dismiss`.
- Produces: `fetchSetupStatus(base: string, token?: string) -> Promise<{dismissed: boolean}>`,
  `dismissSetup(base: string, token?: string) -> Promise<void>` — Task 4/6
  import these two by these exact names.

- [ ] **Step 1: Add to `frontend/src/api.ts`**

```typescript
export async function fetchSetupStatus(
  base: string,
  token?: string,
): Promise<{ dismissed: boolean }> {
  try {
    const res = await fetch(`${base}/setup-status`, { headers: authHeaders(token) });
    if (!res.ok) return { dismissed: false }; // fail closed — never skip the wizard by accident
    return (await res.json()) as { dismissed: boolean };
  } catch {
    return { dismissed: false };
  }
}

export async function dismissSetup(base: string, token?: string): Promise<void> {
  const res = await fetch(`${base}/setup-status/dismiss`, {
    method: "POST",
    headers: authHeaders(token),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.error || `Failed to dismiss setup (${res.status})`);
  }
}
```

Note the deliberate contrast with `fetchCapabilities`'s fail-*open* default
(Global Constraints) — `fetchSetupStatus` fails closed on both a non-OK
response and a thrown fetch error. `dismissSetup` throws on failure, matching
`updateConfig`'s existing pattern (a dismiss failure must be visible to the
admin, not silently swallowed).

- [ ] **Step 2: Add a mock fixture to `frontend/src/mock.ts`**

Add `MOCK_SETUP_STATUS: { dismissed: boolean } = { dismissed: true }` (mock
mode always shows the normal app shell — Global Constraints: the wizard
never appears in mock mode). Follow this file's existing naming/export
convention for other `MOCK_*` fixtures.

- [ ] **Step 3: Typecheck**

Run: `npx tsc -b` (in `frontend/`)
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api.ts frontend/src/mock.ts
git commit -m "feat: add fetchSetupStatus/dismissSetup and a mock fixture"
```

---

### Task 4: Frontend — `SetupWizard` component

**Files:**
- Create: `frontend/src/components/SetupWizard.tsx`

**Interfaces:**
- Consumes: Task 3's `dismissSetup`; the existing `fetchConfig` (from
  `api.ts`, already shipped), `ConnectorCard` (`integration`/`apiBase`/
  `token` props, already shipped), `DeploymentConfigPanel` (already shipped),
  `useIntegrationsHealth()` (already shipped, returns `{health: {integrations:
  IntegrationStatus[]}, ...}` per `frontend/src/types.ts`), and `Capabilities`
  (`connectors: {clio, estateably, quickbooks, graph}`, already shipped).
- Produces: `SetupWizard({ apiBase, token, capabilities, setupResult,
  onDismissed }: { apiBase: string; token?: string; capabilities:
  Capabilities; setupResult: string | null; onDismissed: () => void })` —
  Task 6 renders this component by this exact name and prop shape.

There's no test infrastructure for React components in this codebase (same
as `DeploymentConfigPanel`'s own plan) — verification here is `tsc`/`build`
plus a real dev-server visual check, not TDD.

- [ ] **Step 1: Build the step-list generation**

Inside the component, derive the ordered step list from `capabilities.connectors`:
`(["clio", "estateably", "quickbooks", "graph"] as const).filter((name) => capabilities.connectors[name])`,
each becoming a step whose content is `<ConnectorCard integration={name} apiBase={apiBase} token={token} />`.
Append exactly one more step, "Business Config", whose content is
`<DeploymentConfigPanel apiBase={apiBase} token={token} isAdmin mock={false} />`
(the wizard is only ever rendered for an admin — Global Constraints — so
`isAdmin` is always `true` here, never a prop passed through).

- [ ] **Step 2: Per-step "already done" detection**

For a connector step: `useIntegrationsHealth().health.integrations.some((row) => row.integration === name)`
— a row's mere presence means done, regardless of `row.ok` (Global
Constraints). For the business-config step: on mount, call `fetchConfig(apiBase, token)`
once and check whether any field's `source === "configured"`.

- [ ] **Step 3: Render the wizard shell**

A full-screen layout (no sidebar, no topbar from the main app shell) with: a
step/progress indicator (your call on exact visuals — follow the existing
Setup-screen/connector-card visual language), the current step's content, a
**"Skip for now"** button (advances to the next step without requiring the
step's form to be submitted), and a **"Continue"** button once the step is
done (either just-submitted or already-done per Step 2) — the last step's
button reads **"Finish setup"**. Both "Skip for now" past the last step and
"Finish setup" call `dismissSetup(apiBase, token)` then `onDismissed()`
(surface a `dismissSetup` failure in a `banner-error`, matching this
codebase's existing error-banner convention, and do not call `onDismissed()`
if it fails).

- [ ] **Step 4: Handle a zero-connector-step deployment (Review Focus)**

When every connector is disabled (`capabilities.connectors` all `false`),
the step list is just the one business-config step — verify by inspection
that the component doesn't assume at least one connector step exists
(no off-by-one on the initial step index, no empty-state crash).

- [ ] **Step 5: Surface the QuickBooks `setupResult` banner inside the wizard**

When `setupResult === "quickbooks-connected"` or `"quickbooks-error"`, and
the current/QuickBooks step is showing, render the same banner text
`IntegrationsPanel.tsx` already uses for this (`"QuickBooks connected —
validated."` / the connection-didn't-complete message) — copy the exact
strings from that file rather than paraphrasing. If a QuickBooks step
doesn't exist in this deployment's step list (QuickBooks disabled) but
`setupResult` is set anyway, show the banner on whichever step currently
has focus rather than crashing on a missing step index.

- [ ] **Step 6: Typecheck and build**

Run: `npx tsc -b` and `npm run build` (in `frontend/`).
Expected: both clean.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/SetupWizard.tsx
git commit -m "feat: add the SetupWizard component"
```

---

### Task 5: Frontend — wire into `App.tsx`

**Files:**
- Modify: `frontend/src/App.tsx`

**Interfaces:**
- Consumes: Task 3's `fetchSetupStatus`, Task 4's `SetupWizard`.
- Produces: nothing later tasks consume — this is the integration point.

- [ ] **Step 1: Fetch the dismissed-flag, fail closed**

Add a `dismissed` state, defaulting to `false` (Global Constraints: fail
closed while unresolved). Mirror the existing `capabilities` `useEffect`
exactly (skip the fetch when `mock` is true — Task 3's `MOCK_SETUP_STATUS`
value of `{dismissed: true}` is what mock mode should behave as; for `mock`,
initialize the `dismissed` state to `true` directly rather than fetching, the
same way `ALL_CAPABILITIES` is the mock-mode default for capabilities).

- [ ] **Step 2: Branch inside the existing providers, not before them**

**Corrected during Task 4's implementation (plan defect caught before any
code was written):** the wizard needs `useIntegrationsHealth()` — both
directly (its own "already done" check) and indirectly, since
`ConnectorCard`'s `ClioCard`/`EstateablyCard`/`GraphCard` all call that hook
too. The hook throws with no `IntegrationsHealthProvider` above it in the
tree. Returning the wizard *before* the existing
`<SearchJobsProvider><IntegrationsHealthProvider>` wrap (as originally
written here) would render it with no provider at all. A second,
wizard-only provider instance was considered and rejected — it would mean a
duplicate, separately-polling health fetch that gets thrown away the moment
the wizard dismisses into the real shell, which already has its own
instance.

**Fix:** keep the existing provider wrap exactly as-is; branch *inside* it,
in place of the `<div className="shell">...` content, not before the whole
return:

```tsx
return (
  <SearchJobsProvider apiBase={apiBase} token={token} mock={mock}>
    <IntegrationsHealthProvider apiBase={apiBase} token={token} mock={mock}>
      {!mock && !dismissed ? (
        !isAdmin ? (
          <SetupPending onSignOut={onSignOut} />
        ) : (
          <SetupWizard
            apiBase={apiBase}
            token={token}
            capabilities={capabilities}
            setupResult={setupResult}
            onDismissed={() => setDismissed(true)}
          />
        )
      ) : (
        <div className={`shell${collapsed ? " is-collapsed" : ""}`}>
          {/* ...unchanged existing shell JSX... */}
        </div>
      )}
    </IntegrationsHealthProvider>
  </SearchJobsProvider>
);
```

`SetupWizard` (Task 4) calls `useIntegrationsHealth()` directly, exactly as
originally briefed — it does **not** wrap itself in its own provider; this
corrected structure guarantees one is already above it.

Define `SetupPending` as a small inline component in this same file (it's a
handful of lines — a centered message plus a sign-out button; no new file
needed) rather than importing it from elsewhere, since no other task
produces it.

- [ ] **Step 3: Stop forcing `activeTab` to `"integrations"` unconditionally**

The existing `readSetupResult()`-driven `activeTab` initializer
(`readSetupResult() ? "integrations" : DEFAULT_TAB`) stays as the fallback
for when the wizard *isn't* showing (Global Constraints: the cold-reload
banner now lives inside the wizard when `!dismissed`, but once `dismissed`
is `true` — e.g., an admin reconnecting QuickBooks from the ordinary
Integrations tab after initial setup — the existing behavior of landing on
Integrations to show the banner is still correct and must not regress).

- [ ] **Step 4: Typecheck and build**

Run: `npx tsc -b` and `npm run build` (in `frontend/`).
Expected: both clean.

- [ ] **Step 5: Manually verify in the dev server (Review Focus)**

Start the dev server (`npm run dev`), and since mock mode always shows the
normal shell (Task 3's fixture), temporarily force `dismissed` to `false`
and `isAdmin` to `true` the same way earlier work in this codebase
temporarily forced `isAdmin` in `main.tsx` for manual verification — confirm
the wizard renders, "Skip for now" advances through all steps, and
"Finish setup" returns to the normal shell. Then temporarily force `isAdmin`
to `false` with `dismissed` still `false` and confirm the non-admin message
renders instead. Revert the temporary changes before committing (`git diff`
must be clean except for this task's actual files).

- [ ] **Step 6: Commit**

```bash
git add frontend/src/App.tsx
git commit -m "feat: show the first-run setup wizard on a fresh deployment"
```
