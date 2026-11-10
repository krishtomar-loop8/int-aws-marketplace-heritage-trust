# Modular, Toggleable Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a buyer independently enable/disable each of the four
connectors and the three ancillary modules (Quarterly Reports, AML
Export, Sanction Screener) via Terraform variables, with an invalid
combination (a module enabled without the connector it needs) rejected
at `terraform validate`/`plan` time, Matter Search degrading honestly to
whichever connectors exist, and the frontend hiding a disabled module's
tab.

**Architecture:** Eight new boolean Terraform variables with
cross-variable `validation` blocks; conditional `count` on the
Quarterly Report/AML Export/Sanctions Refresh modules; conditional IAM
statements (not conditional secrets — a disabled connector is paused,
never destroyed) in `search_api`; four new per-connector env vars plus
reuse of two existing per-module env vars in `_build_deps`; a new
`disabled_sources` field threaded from the orchestrator through to the
synthesis prompt; a new `GET /capabilities` route; frontend nav/health
filtering driven by that route.

**Tech Stack:** Terraform (HCL), Python 3.13 (Lambda), pytest,
React/TypeScript.

**Spec:** `docs/superpowers/specs/2026-11-10-modular-toggleable-architecture-design.md`

## Global Constraints

- A disabled connector is **paused, not destroyed** — the Secrets
  Manager secret resource stays unconditional; only IAM read/write
  grants and the connect/reconnect routes become conditional (Decision 1).
- Enabling `quarterly_enabled`/`aml_export_enabled` without their
  required connector(s) must fail at `terraform validate`, never at
  runtime.
- Matter Search is never toggleable — only which connectors feed it
  varies.
- A disabled module's list/download API routes must become genuinely
  unreachable (503), not just hidden in the frontend nav (Decision 2).
- No credential, token, or secret value is ever logged.
- Every new AWS resource picks up `Project = aphrodite` automatically
  via the provider's `default_tags` — no per-resource tagging needed.

## Review Focus

- `quarterly_enabled = true` with `clio_enabled = false`: must fail
  `terraform validate`, not silently deploy a broken module.
- `aml_export_enabled = true` with only one of `clio_enabled`/
  `estateably_enabled` true (not both): must also fail — the dependency
  is on both, not either.
- All four connectors disabled at once (Matter Search can't be turned
  off, so this combination is reachable): Matter Search must still
  respond, not crash, with an honest "no sources available" answer
  rather than a 500.
- Pausing a connector that already has a stored, working credential and
  re-enabling it later: the credential must still work without the
  admin reconnecting (proves Decision 1 isn't accidentally destructive).
- An admin tries to reconnect a *disabled* connector through the
  existing `/connectors/*` routes: must be rejected, not silently
  accepted into a secret nothing will ever read.

---

### Task 1: Terraform — enable flags + dependency validation

**Files:**
- Modify: `terraform/variables.tf`

**Interfaces:**
- Produces: `var.clio_enabled`, `var.estateably_enabled`,
  `var.quickbooks_enabled`, `var.graph_enabled` (all `bool`, default
  `true`); `var.quarterly_enabled`, `var.aml_export_enabled`,
  `var.sanction_screener_enabled` (all `bool`, default `false`) — every
  later task's Terraform and backend work reads these by name.

- [ ] **Step 1: Add the four connector flags and three module flags with validation**

In `terraform/variables.tf`, in a new `# -- Modular architecture (Phase 2)
--` section:

```hcl
variable "clio_enabled" {
  type        = bool
  default     = true
  description = "Whether this deployment connects to Clio."
}
variable "estateably_enabled" {
  type        = bool
  default     = true
  description = "Whether this deployment connects to Estateably."
}
variable "quickbooks_enabled" {
  type        = bool
  default     = true
  description = "Whether this deployment connects to QuickBooks."
}
variable "graph_enabled" {
  type        = bool
  default     = true
  description = "Whether this deployment connects to Microsoft Graph (Outlook/SharePoint)."
}

variable "quarterly_enabled" {
  type        = bool
  default     = false
  description = "Whether the Quarterly Report Draft Generator module is deployed."
  validation {
    condition     = !var.quarterly_enabled || var.clio_enabled
    error_message = "quarterly_enabled requires clio_enabled (Quarterly Reports reads Clio exclusively)."
  }
}
variable "aml_export_enabled" {
  type        = bool
  default     = false
  description = "Whether the Monthly AML Name Export module is deployed."
  validation {
    condition     = !var.aml_export_enabled || (var.clio_enabled && var.estateably_enabled)
    error_message = "aml_export_enabled requires both clio_enabled and estateably_enabled."
  }
}
variable "sanction_screener_enabled" {
  type        = bool
  default     = false
  description = "Whether the Sanction Screener module (and its monthly watchlist refresh) is deployed."
}
```

- [ ] **Step 2: Verify**

Run: `terraform -chdir=terraform fmt -check -recursive && terraform -chdir=terraform validate`
Expected: `Success! The configuration is valid` (same pre-existing
deprecation warnings only, no new errors — none of these variables are
consumed anywhere yet, so nothing else changes).

- [ ] **Step 3: Prove both validation blocks actually fire**

In an isolated scratch directory (outside the real backend, which needs
AWS access this account may not have), write a minimal reproduction of
both validation blocks.

Run: `terraform plan -var='quarterly_enabled=true' -var='clio_enabled=false'`
Expected: `Error: Invalid value for variable ... quarterly_enabled
requires clio_enabled`.

Run: `terraform plan -var='aml_export_enabled=true' -var='clio_enabled=true'
-var='estateably_enabled=false'`
Expected: `Error: Invalid value for variable ... aml_export_enabled
requires both clio_enabled and estateably_enabled` (confirms the
dependency is on *both* connectors, not just one).

- [ ] **Step 4: Commit**

```bash
git add terraform/variables.tf
git commit -m "feat: add connector/module enable flags with dependency validation"
```

---

### Task 2: Terraform — conditional modules, conditional IAM, new env vars

**Files:**
- Modify: `terraform/main.tf`
- Modify: `terraform/modules/search_api/main.tf`
- Modify: `terraform/modules/search_api/variables.tf`

**Interfaces:**
- Consumes: Task 1's seven variables.
- Produces: four new Lambda env vars (`APHRODITE_CLIO_ENABLED`,
  `APHRODITE_ESTATEABLY_ENABLED`, `APHRODITE_QUICKBOOKS_ENABLED`,
  `APHRODITE_GRAPH_ENABLED`, each the string `"true"`/`"false"`) and one
  more (`APHRODITE_SANCTION_SCREENER_ENABLED`) — Task 3 reads all five
  by exact name.

- [ ] **Step 1: Make the three ancillary modules conditional in `terraform/main.tf`**

Add `count = var.quarterly_enabled ? 1 : 0` to `module "quarterly_report"`,
`count = var.aml_export_enabled ? 1 : 0` to `module "aml_export"`, and
`count = var.sanction_screener_enabled ? 1 : 0` to `module
"sanctions_refresh"`.

Update every cross-reference to these modules elsewhere in `main.tf`'s
`module "search_api"` block to a conditional expression (never a bare
`module.quarterly_report.X` — that errors once the module has `count`):

```hcl
quarterly_function_name = var.quarterly_enabled ? module.quarterly_report[0].function_name : ""
quarterly_function_arn  = var.quarterly_enabled ? module.quarterly_report[0].function_arn : ""
aml_export_function_name = var.aml_export_enabled ? module.aml_export[0].function_name : ""
aml_export_function_arn  = var.aml_export_enabled ? module.aml_export[0].function_arn : ""
aml_schedule_name        = var.aml_export_enabled ? module.aml_export[0].schedule_name : ""
aml_schedule_arn         = var.aml_export_enabled ? module.aml_export[0].schedule_arn : ""
aml_scheduler_role_arn   = var.aml_export_enabled ? module.aml_export[0].scheduler_role_arn : ""
```

(This already matches how `_build_deps` treats an empty string as "not
configured" for `quarterly_fn`/`aml_export_fn` today — no backend change
needed for this part; see Task 3.)

- [ ] **Step 2: Make the connector secret IAM grants conditional, not the secrets themselves**

In `terraform/main.tf`'s `module "search_api"` block, change the four
secret-ARN passthroughs to resolve to an empty string when disabled
(the secret itself — `module.secrets.secret_arns["clio"]` etc. — is
untouched; only what gets passed to IAM-grant-construction changes):

```hcl
clio_secret_arn       = var.clio_enabled ? module.secrets.secret_arns["clio"] : ""
estateably_secret_arn = var.estateably_enabled ? module.secrets.secret_arns["estateably"] : ""
quickbooks_secret_arn = var.quickbooks_enabled ? module.secrets.secret_arns["quickbooks"] : ""
graph_secret_arn       = var.graph_enabled ? module.secrets.secret_arns["graph"] : ""
```

In `terraform/modules/search_api/main.tf`'s `data.aws_iam_policy_document.search`,
wrap each of the four `Write*Token`/`Write*Credential` statements (`WriteQuickBooksToken`,
`WriteClioToken`, `WriteEstateablyCredential`, `WriteGraphCredential`) in
a `dynamic "statement"` keyed on its ARN being non-empty, e.g.:

```hcl
dynamic "statement" {
  for_each = var.clio_secret_arn != "" ? [1] : []
  content {
    sid       = "WriteClioToken"
    actions   = ["secretsmanager:PutSecretValue"]
    resources = [var.clio_secret_arn]
  }
}
```

Change the existing `ReadSecrets` statement's `resources = var.secret_arns`
to filter out empty-string ARNs too:
`resources = [for arn in var.secret_arns : arn if arn != ""]` — and in
root `main.tf`, build `secret_arns` the same conditional way instead of
`values(module.secrets.secret_arns)` (unconditional today): `secret_arns
= [for k, v in {clio = var.clio_enabled, estateably = var.estateably_enabled,
quickbooks = var.quickbooks_enabled, graph = var.graph_enabled} : module.secrets.secret_arns[k] if v]`.

- [ ] **Step 3: Wire the five new Lambda env vars**

In `terraform/modules/search_api/main.tf`'s `aws_lambda_function.search`
environment block, add:

```hcl
APHRODITE_CLIO_ENABLED               = var.clio_enabled
APHRODITE_ESTATEABLY_ENABLED         = var.estateably_enabled
APHRODITE_QUICKBOOKS_ENABLED         = var.quickbooks_enabled
APHRODITE_GRAPH_ENABLED              = var.graph_enabled
APHRODITE_SANCTION_SCREENER_ENABLED  = var.sanction_screener_enabled
```

(Terraform interpolates a bare bool into an env var map as the string
`"true"`/`"false"`.) Add the five corresponding variables
(`clio_enabled`, `estateably_enabled`, `quickbooks_enabled`,
`graph_enabled`, `sanction_screener_enabled`, all `type = bool`) to
`terraform/modules/search_api/variables.tf`, and pass them through from
root `main.tf`'s `module "search_api"` block.

- [ ] **Step 4: Verify**

Run: `terraform -chdir=terraform fmt -check -recursive && terraform -chdir=terraform validate`
Expected: `Success! The configuration is valid` (same pre-existing
warnings only).

- [ ] **Step 5: Confirm the secret resources themselves stayed unconditional (Review Focus)**

`terraform/modules/secrets/` is blocked by this session's permission
settings (same restriction hit on the connector-onboarding branch) — verify
without reading it: this task's own diff must touch only
`terraform/main.tf` and `terraform/modules/search_api/`, never anything
under `terraform/modules/secrets/`.

Run: `git diff --stat HEAD -- terraform/modules/secrets/`
Expected: empty output — proves Step 2 added `count`/`for_each` only to
the IAM *statements* consuming each secret ARN, never to the secret
resources themselves, so pausing a connector can never destroy its
stored credential.

- [ ] **Step 6: Commit**

```bash
git add terraform/main.tf terraform/modules/search_api/
git commit -m "feat: make connectors/modules conditionally deployed and wire enabled-flag env vars"
```

---

### Task 3: Backend — `_build_deps` honors the enabled flags

**Files:**
- Modify: `backend/api/handlers.py`
- Test: `backend/api/tests/test_handlers.py`

**Interfaces:**
- Consumes: Task 2's five env vars (`APHRODITE_CLIO_ENABLED`, etc.) plus
  the already-existing `APHRODITE_QUARTERLY_FUNCTION`/`APHRODITE_AML_EXPORT_FUNCTION`.
- Produces: `Deps.connectors_enabled: frozenset[str]` (new field) —
  Task 4 (orchestrator) and Task 5 (`/capabilities`) both read it by
  this exact name.

- [ ] **Step 1: Write the failing tests**

```python
def test_build_deps_skips_disabled_connectors(monkeypatch):
    monkeypatch.setenv("APHRODITE_JOBS_TABLE", "jobs")
    monkeypatch.setenv("APHRODITE_SELF_FUNCTION", "fn")
    monkeypatch.setenv("APHRODITE_ESTATEABLY_ENABLED", "false")
    deps = handlers._build_deps()
    assert "estateably" not in deps.connectors_enabled
    assert "clio" in deps.connectors_enabled  # unset env var defaults to enabled

def test_build_deps_all_connectors_enabled_by_default(monkeypatch):
    monkeypatch.setenv("APHRODITE_JOBS_TABLE", "jobs")
    monkeypatch.setenv("APHRODITE_SELF_FUNCTION", "fn")
    deps = handlers._build_deps()
    assert deps.connectors_enabled == frozenset({"clio", "estateably", "quickbooks", "graph"})

def test_build_deps_reports_disabled_when_quarterly_function_absent(monkeypatch):
    monkeypatch.setenv("APHRODITE_JOBS_TABLE", "jobs")
    monkeypatch.setenv("APHRODITE_SELF_FUNCTION", "fn")
    monkeypatch.setenv("APHRODITE_OUTPUTS_BUCKET", "bucket")
    monkeypatch.delenv("APHRODITE_QUARTERLY_FUNCTION", raising=False)
    deps = handlers._build_deps()
    assert deps.reports is None  # outputs_bucket alone is no longer enough

def test_build_deps_screener_disabled_by_default(monkeypatch):
    monkeypatch.setenv("APHRODITE_JOBS_TABLE", "jobs")
    monkeypatch.setenv("APHRODITE_SELF_FUNCTION", "fn")
    monkeypatch.setenv("APHRODITE_OUTPUTS_BUCKET", "bucket")
    monkeypatch.delenv("APHRODITE_SANCTION_SCREENER_ENABLED", raising=False)
    deps = handlers._build_deps()
    assert deps.screener is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest backend/api/tests/test_handlers.py -k "build_deps_skips or build_deps_all_connectors or reports_disabled or screener_disabled_by_default" -v`
Expected: FAIL — `connectors_enabled` doesn't exist yet on `Deps`;
`deps.reports`/`deps.screener` are not yet `None` in these cases.

- [ ] **Step 3: Implement in `backend/api/handlers.py`**

Add `connectors_enabled: frozenset[str] = frozenset()` as the last field
of `Deps`. In `_build_deps`, before constructing the connectors, add:

```python
def _flag(name: str) -> bool:
    return os.environ.get(name, "true").lower() != "false"

connectors_enabled = frozenset(
    name
    for name, env in (
        ("clio", "APHRODITE_CLIO_ENABLED"),
        ("estateably", "APHRODITE_ESTATEABLY_ENABLED"),
        ("quickbooks", "APHRODITE_QUICKBOOKS_ENABLED"),
        ("graph", "APHRODITE_GRAPH_ENABLED"),
    )
    if _flag(env)
)
```

Change the four connector constructions to
`estateably = EstateablyConnector(secrets) if "estateably" in connectors_enabled else None`
(same shape for clio/quickbooks/graph). Move the existing
`quarterly_fn = os.environ.get("APHRODITE_QUARTERLY_FUNCTION")` and
`aml_export_fn = os.environ.get("APHRODITE_AML_EXPORT_FUNCTION")` reads
earlier (immediately before `reports`/`aml_exports` are built), and
change those two constructions to also require the function name:
`reports = ReportsService(s3, outputs_bucket) if s3 and quarterly_fn else None`,
`aml_exports = AmlExportsService(s3, outputs_bucket) if s3 and aml_export_fn else None`.
Add `screener_enabled = _flag("APHRODITE_SANCTION_SCREENER_ENABLED")` —
but default this one to `"false"` (`os.environ.get(name, "false")`), not
`"true"`, matching the variable's own `default = false` — and require it
in the existing `screener = ScreenerService(...) if s3 and outputs_bucket
and screener_enabled else None` condition. Pass `connectors_enabled=connectors_enabled`
into the final `Deps(...)` call.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest backend/api/tests/test_handlers.py -v`
Expected: all pass.

- [ ] **Step 5: Gate the connector-onboarding `/connectors/*` routes on the same flag (Review Focus)**

Write the failing test first:

```python
def test_connect_estateably_route_rejects_disabled_connector(monkeypatch: pytest.MonkeyPatch) -> None:
    install_deps(monkeypatch)
    handlers._DEPS.connectors_enabled = frozenset({"clio"})  # estateably disabled
    resp = handlers.handler(
        admin_api_event("POST", "/connectors/estateably", body={"username": "u", "password": "p"})
    )
    assert resp["statusCode"] == 503
```

Run it, confirm it fails (route currently only checks `_require_admin`),
then in each of the five `/connectors/*` route bodies in `handler()`, add
a check before the existing admin check: `if "<connector>" not in
deps.connectors_enabled: return _response(503, {"error": "<connector>
disabled for this deployment"})` (the QuickBooks callback route checks
`"quickbooks"` too, even though it's unauthenticated — a disabled
connector's callback should still 503, not silently process a stale
link). Run the full suite again to confirm green.

- [ ] **Step 6: Commit**

```bash
git add backend/api/handlers.py backend/api/tests/test_handlers.py
git commit -m "feat: gate connector construction and routes on per-deployment enabled flags"
```

---

### Task 4: Backend — Matter Search distinguishes disabled from failed

**Files:**
- Modify: `backend/search/orchestrator.py`
- Modify: `backend/search/framework.py`
- Test: `backend/search/tests/test_orchestrator.py`
- Test: `backend/search/tests/test_framework.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (this is pure dossier/prompt logic,
  exercised directly — `DossierBuilder`'s connector params already
  default to `None`, per the design doc's corrected Current State).
- Produces: `MatterDossier.disabled_sources: frozenset[str]` — included
  in `as_dict()`'s output as `"disabled_sources"`.

- [ ] **Step 1: Write the failing test for the dossier field**

```python
def test_build_marks_disabled_connectors_distinct_from_candidate_gaps():
    builder = DossierBuilder(estateably=None, clio=FakeClio(), quickbooks=None, graph=None)
    candidate = MatterCandidate(name="Smith Estate", refs=[SourceRef("clio", "1", "Smith")], matter_type="estate", score=1.0)
    dossier = builder.build(candidate)
    assert dossier.disabled_sources == frozenset({"estateably", "quickbooks", "graph"})
    assert "clio" not in dossier.disabled_sources


def test_build_with_all_connectors_disabled_does_not_crash():
    # Review Focus: Matter Search itself is never toggleable, so this
    # combination (every connector off) is reachable and must degrade
    # honestly rather than raise.
    builder = DossierBuilder(estateably=None, clio=None, quickbooks=None, graph=None)
    candidate = MatterCandidate(name="Smith Estate", refs=[SourceRef("clio", "1", "Smith")], matter_type="estate", score=1.0)
    dossier = builder.build(candidate)
    assert dossier.disabled_sources == frozenset({"estateably", "clio", "quickbooks", "graph"})
    assert dossier.errors == []  # nothing was attempted, so nothing failed
    assert dossier.as_dict()["disabled_sources"]
```

(Adapt `FakeClio`/`MatterCandidate`/`SourceRef` construction to this
test file's existing fixtures — check the file for its established
pattern before writing this literally.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest backend/search/tests/test_orchestrator.py -k "disabled_connectors or all_connectors_disabled" -v`
Expected: FAIL — `disabled_sources` doesn't exist on `MatterDossier` yet.

- [ ] **Step 3: Implement**

Add `disabled_sources: frozenset[str] = field(default_factory=frozenset)`
to `MatterDossier` (`orchestrator.py`), and include it in `as_dict()`'s
returned dict as `"disabled_sources": sorted(self.disabled_sources)`. In
`DossierBuilder.build()`, near the top (before the `ref_ids`-gated job
list is built), set it independent of any one candidate's refs:

```python
dossier.disabled_sources = frozenset(
    name
    for name, connector in (
        ("estateably", self._estateably),
        ("clio", self._clio),
        ("quickbooks", self._quickbooks),
        ("graph", self._graph),
    )
    if connector is None
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest backend/search/tests/test_orchestrator.py -v`
Expected: all pass.

- [ ] **Step 5: Write the failing test for the prompt instruction**

```python
def test_framework_distinguishes_disabled_sources_from_gaps():
    text = render_framework()
    assert "disabled_sources" in text
```

- [ ] **Step 6: Run test to verify it fails**

Run: `pytest backend/search/tests/test_framework.py -k disabled_sources -v`
Expected: FAIL — the framework template never mentions `disabled_sources`.

- [ ] **Step 7: Update the prompt text in `backend/search/framework.py`**

In the `insufficient_info` rule (the paragraph starting "Set
`insufficient_info` to true ONLY when a core source..."), add: a source
listed in the dossier's `disabled_sources` was never queried by design —
never treat it as a gap, never let it alone set `insufficient_info`, and
describe it (where relevant) as "not used by this deployment," not as
unavailable or failed. In the citations section's "If a source ... is
absent from the dossier, state that it was not available" sentence, add
the same distinction: a `disabled_sources` entry is described as not used
by this deployment, never as "not available."

- [ ] **Step 8: Run test to verify it passes, then the whole suite**

Run: `pytest backend/search/tests/test_framework.py backend/search/tests/test_orchestrator.py -v`
Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add backend/search/orchestrator.py backend/search/framework.py \
        backend/search/tests/test_orchestrator.py backend/search/tests/test_framework.py
git commit -m "feat: distinguish intentionally-disabled sources from genuine gaps in search"
```

---

### Task 5: Backend — `GET /capabilities`

**Files:**
- Modify: `backend/api/handlers.py`
- Test: `backend/api/tests/test_handlers.py`

**Interfaces:**
- Consumes: `deps.connectors_enabled` (Task 3), `deps.reports is None` /
  `deps.aml_exports is None` / `deps.screener is None` (already correct
  after Task 3).
- Produces: `GET /capabilities` → `{"modules": {"quarterly_reports": bool,
  "aml_export": bool, "sanction_screener": bool}, "connectors": {"clio": bool,
  "estateably": bool, "quickbooks": bool, "graph": bool}}`.

- [ ] **Step 1: Write the failing test**

```python
def test_capabilities_route_reflects_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    deps, _ = install_deps(monkeypatch)
    deps.connectors_enabled = frozenset({"clio"})
    deps.reports = None
    deps.aml_exports = object()
    deps.screener = None
    resp = handlers.handler(api_event("GET", "/capabilities"))
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["connectors"] == {"clio": True, "estateably": False, "quickbooks": False, "graph": False}
    assert body["modules"] == {"quarterly_reports": False, "aml_export": True, "sanction_screener": False}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest backend/api/tests/test_handlers.py -k test_capabilities_route -v`
Expected: FAIL — 404 (no route exists).

- [ ] **Step 3: Implement the route**

Add, following the existing route-dispatch style in `handler()`:

```python
if method == "GET" and path.endswith("/capabilities"):
    connectors = {
        name: name in deps.connectors_enabled
        for name in ("clio", "estateably", "quickbooks", "graph")
    }
    modules = {
        "quarterly_reports": deps.reports is not None,
        "aml_export": deps.aml_exports is not None,
        "sanction_screener": deps.screener is not None,
    }
    return _response(200, {"modules": modules, "connectors": connectors})
```

Behind the same Cognito JWT authorizer as every other route (no change
needed — the authorizer is wired at the API Gateway route level, not per
route in this handler).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest backend/api/tests/test_handlers.py -v`
Expected: all pass.

- [ ] **Step 5: Wire the route in Terraform**

Add `"GET /capabilities"` to the existing `aws_apigatewayv2_route.routes`
`for_each` list in `terraform/modules/search_api/main.tf`. Verify:
`terraform -chdir=terraform fmt -check -recursive && terraform -chdir=terraform validate`.

- [ ] **Step 6: Commit**

```bash
git add backend/api/handlers.py backend/api/tests/test_handlers.py terraform/modules/search_api/main.tf
git commit -m "feat: add GET /capabilities reflecting enabled connectors and modules"
```

---

### Task 6: Frontend — hide disabled module tabs and connector rows

**Files:**
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/components/IntegrationsPanel.tsx`

**Interfaces:**
- Consumes: `GET /capabilities` (Task 5).
- Produces: `fetchCapabilities(base: string, token?: string) ->
  Promise<{modules: Record<string, boolean>; connectors: Record<string, boolean>}>`
  in `api.ts`.

- [ ] **Step 1: Add `fetchCapabilities` to `frontend/src/api.ts`**

Same `authHeaders`/`fetch` pattern as `getIntegrationsHealth` — never
throws, returns a safe "everything enabled" default on any failure (so a
transient fetch error never hides a tab that should be visible):

```typescript
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
```

(No dedicated unit test — no test infrastructure exists for `api.ts`,
same fallback the connector-onboarding branch's Task 9 already used.)

- [ ] **Step 2: Filter the nav in `App.tsx`**

Fetch capabilities once on mount (`useEffect` + `useState`, same shape as
`readSetupResult`'s existing pattern in this file); filter `TABS` before
passing to `Sidebar`:

```typescript
const MODULE_CAPABILITY_KEY: Partial<Record<TabId, keyof Capabilities["modules"]>> = {
  "quarterly-reports": "quarterly_reports",
  "aml-export": "aml_export",
  "sanction-screener": "sanction_screener",
};
const visibleTabs = TABS.filter((t) => {
  const key = MODULE_CAPABILITY_KEY[t.id];
  return !key || capabilities.modules[key];
});
```

Pass `visibleTabs` (not the raw `TABS` import) to `Sidebar`, and use it
for the `active`/fallback lookup too.

- [ ] **Step 3: Filter connector rows in `IntegrationsPanel`**

`IntegrationsPanel` already has a `CONNECTABLE` set (from the
connector-onboarding branch) gating which rows get a connect/reconnect
control. Accept a new `capabilities?: Capabilities` prop; when present,
filter `integrations` (the rows from `useIntegrationsHealth()`) to only
those where `capabilities.connectors[row.integration] !== false` before
rendering the table (a connector the backend never asked about —
`sharepoint`, if it's ever a distinct row — stays visible by default).

- [ ] **Step 4: Typecheck and build**

Run: `npx tsc -b` (in `frontend/`) and `npm run build`.
Expected: both clean.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api.ts frontend/src/App.tsx frontend/src/components/IntegrationsPanel.tsx
git commit -m "feat: hide disabled modules and connectors in the frontend"
```

---

### Task 7: Documentation

**Files:**
- Modify: `terraform/README.md` (check it exists first; if not, add a
  short new file documenting just this toggle matrix)
- Modify: `backend/README.md`

**Interfaces:**
- Consumes: the final variable/env-var names from Tasks 1-5.

- [ ] **Step 1: Document the toggle matrix**

Add a table listing each of the seven flags, its default, and its
dependency (e.g. "aml_export_enabled — requires clio_enabled AND
estateably_enabled").

- [ ] **Step 2: Document the five new env vars in `backend/README.md`**

Same table style as the existing "Search configuration" / "Connector
onboarding & reconnect" sections.

- [ ] **Step 3: Verify**

Run: `grep -rn "aml_export_enabled\|APHRODITE_SANCTION_SCREENER_ENABLED" terraform/README.md backend/README.md`
Expected: at least one match each.

- [ ] **Step 4: Commit**

```bash
git add terraform/README.md backend/README.md
git commit -m "docs: document the modular architecture toggle matrix"
```
