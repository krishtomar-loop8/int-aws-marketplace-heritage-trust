# Connector Onboarding & Reconnect Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a customer admin connect, and later reconnect, all four source
systems (Clio, QuickBooks, Estateably, Graph/SharePoint) from inside the app,
with live validation and alerting, without a Loop8 engineer running a script
on their behalf.

**Architecture:** A new `backend/api/connectors_admin.py` service module holds
the validate-then-store logic per connector (mirroring the existing
`aml_admin.py`/`screener.py` pattern); `backend/api/handlers.py` gains five
new routes (four admin-gated, one public OAuth callback) that call into it.
`backend/api/integrations.py`'s existing health store gains a threshold-based
alert hook. Two existing connectors (`estateably`, `graph`) get the same
stale-secret hardening Clio/QuickBooks already have. The frontend extends the
existing (today read-only) `IntegrationsPanel.tsx` into an actionable screen,
gated by a new Cognito admin group.

**Tech Stack:** Python 3.13 (Lambda), pytest + `responses` for HTTP mocking,
Terraform (API Gateway v2, Cognito, SNS), React/TypeScript, `react-oidc-context`.

**Spec:** `docs/superpowers/specs/2026-11-06-connector-onboarding-design.md`

## Global Constraints

- No credential (password, client_secret, auth code, token) ever appears in a
  log line, in any new code — same discipline as the existing OAuth bootstrap
  scripts ("Credentials never pass through the command line or this program's
  output").
- A stored secret's value is never returned by any new route — write-only.
- A failed credential validation leaves the existing secret untouched (no
  partial/garbage write) and surfaces a clear error — never a silent fallback
  to stale credentials.
- Every new route that **writes** credentials requires membership in the
  `admins` Cognito user-pool group; the public QuickBooks callback route is
  the one exception (GET, no admin header possible — protected instead by the
  `state` value minted by the admin-gated `start` route).
- All reads/writes stay read-only toward Clio/Estateably/QuickBooks/Graph
  themselves — these routes write only to this deployment's own Secrets
  Manager, never back to a source system.
- Every new AWS resource picks up `Project = aphrodite` automatically via the
  provider's `default_tags` — no per-resource tagging needed.

## Review Focus

- A non-admin authenticated user calls a `/connectors/*` write route directly
  (not through the UI): must get `403`, never a silent success.
- A credential validation fails (wrong password, bad client_secret, wrong
  tenant_id): Secrets Manager must be unchanged afterward, not left holding a
  half-written or garbage secret.
- The QuickBooks callback is hit with a `state` that doesn't match what
  `quickbooks/start` minted (stale link, replay, a second browser tab): the
  exchange must not complete.
- Re-submitting valid credentials for an already-healthy connector (a
  "reconnect" with no real problem) must not crash, double-count a health
  transition, or fire a spurious alert.
- The SNS alert fires exactly once per outage (at `consecutive_failures ==
  ALERT_THRESHOLD`), not on the first blip and not again on every subsequent
  hourly failure of the same outage.

---

### Task 1: Admin authorization helper + Cognito group

**Files:**
- Modify: `backend/api/handlers.py` (near `_actor`, ~line 245)
- Test: `backend/api/tests/test_handlers.py`
- Modify: `terraform/modules/cognito/main.tf`

**Interfaces:**
- Produces: `backend.api.handlers.ADMIN_GROUP_NAME: str = "admins"`,
  `backend.api.handlers._is_admin(event: dict[str, Any]) -> bool`,
  `backend.api.handlers._require_admin(event: dict[str, Any]) -> dict[str, Any] | None`
  (returns a `403` response dict when not admin, else `None`) — every later
  task's admin-gated routes call this first.

- [ ] **Step 1: Write the failing tests**

```python
def _event_with_groups(groups_claim) -> dict:
    return {"requestContext": {"authorizer": {"jwt": {"claims": {"cognito:groups": groups_claim}}}}}

def test_is_admin_true_for_json_array_claim():
    assert handlers._is_admin(_event_with_groups('["admins"]')) is True

def test_is_admin_true_for_plain_string_claim():
    assert handlers._is_admin(_event_with_groups("admins")) is True

def test_is_admin_false_for_other_group():
    assert handlers._is_admin(_event_with_groups('["staff"]')) is False

def test_is_admin_false_when_claim_missing():
    assert handlers._is_admin({"requestContext": {"authorizer": {"jwt": {"claims": {}}}}}) is False

def test_is_admin_false_when_claims_missing_entirely():
    assert handlers._is_admin({}) is False

def test_require_admin_returns_403_body_for_non_admin():
    resp = handlers._require_admin({})
    assert resp is not None and resp["statusCode"] == 403

def test_require_admin_returns_none_for_admin():
    assert handlers._require_admin(_event_with_groups('["admins"]')) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest backend/api/tests/test_handlers.py -k is_admin -v`
Expected: FAIL with `AttributeError: module ... has no attribute '_is_admin'`

- [ ] **Step 3: Implement `ADMIN_GROUP_NAME`, `_is_admin`, `_require_admin` in `backend/api/handlers.py`**

`_is_admin` reads `claims.get("cognito:groups", "")`; if it starts with `"["`,
`json.loads` it (API Gateway's JWT authorizer serializes a multi-value claim
as a JSON-array string), else treat the raw string as a single group name;
on any parse failure treat it as a single group name. Membership test is
`ADMIN_GROUP_NAME in groups`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest backend/api/tests/test_handlers.py -k is_admin -v`
Expected: PASS, 7/7

- [ ] **Step 5: Add the Cognito group in Terraform**

In `terraform/modules/cognito/main.tf`, add:

```hcl
resource "aws_cognito_user_pool_group" "admins" {
  name         = "admins"
  user_pool_id = aws_cognito_user_pool.main.id
  description  = "Staff who may connect/reconnect source-system credentials (Setup screen)."
}
```

No new variable needed — the group name is fixed (matches `ADMIN_GROUP_NAME`).
A federated (SAML) user gets a local Cognito shadow record after their first
sign-in; adding that record to this group (console or
`aws cognito-idp admin-add-user-to-group`) is a manual, documented step —
not automated by this plan (same "assisted self-service" posture as the rest
of onboarding).

- [ ] **Step 6: Verify Terraform**

Run: `terraform -chdir=terraform fmt -check -recursive && terraform -chdir=terraform validate`
Expected: `Success! The configuration is valid` (same pre-existing deprecation
warnings as before, no new errors)

- [ ] **Step 7: Commit**

```bash
git add backend/api/handlers.py backend/api/tests/test_handlers.py terraform/modules/cognito/main.tf
git commit -m "feat: add admin-group authorization check for connector routes"
```

---

### Task 2: Harden Estateably and Graph against stale cached secrets

**Files:**
- Modify: `backend/connectors/estateably/connector.py`
- Modify: `backend/connectors/graph/connector.py`
- Test: `backend/connectors/estateably/tests/test_connector.py`
- Test: `backend/connectors/graph/tests/test_connector.py`

**Interfaces:**
- Produces: `EstateablyConnector._credentials(self, *, refresh: bool = False)`,
  `EstateablyConnector._login(self, *, refresh_secret: bool = False)`;
  `GraphConnector._credentials(self, *, refresh: bool = False)`,
  `GraphConnector._fetch_token(self, *, refresh_secret: bool = False)`. No
  other task depends on these signatures directly (Task 4/5 call the
  connectors' existing public methods only).

- [ ] **Step 1: Write the failing test (Estateably)**

```python
def test_401_after_reconnect_uses_fresh_secret_not_cache():
    # Simulates: container warm with old creds, admin reconnects (secret
    # changes in Secrets Manager), next call 401s on the stale cached creds,
    # and the retry must re-read the secret rather than reusing the cache.
    secrets = FakeSecrets({"username": "old", "password": "old-pw"})
    connector = EstateablyConnector(secrets)
    register_login(token="t1")  # old creds login succeeds once
    connector._login()
    secrets._secret = {"username": "new", "password": "new-pw"}  # admin reconnected
    responses.add(responses.GET, f"{DEFAULT_BASE_URL}/api/v1/globalSearch",
                   json={"error": "unauthorized"}, status=401)
    register_login(token="t2")  # only matches if login call used the NEW creds
    responses.add(responses.GET, f"{DEFAULT_BASE_URL}/api/v1/globalSearch",
                   json={"data": []}, status=200)
    connector.global_search("x")
    assert secrets.get_calls[-1] == {"refresh": True}
```

(Adapt `register_login`/response matching to this file's existing helpers —
the assertion that matters is that the retry path calls
`secrets.get(..., refresh=True)`, which `FakeSecrets` must be extended to
record, same spirit as Clio's `FakeSecretsClient.puts` list.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest backend/connectors/estateably/tests/test_connector.py -k reconnect -v`
Expected: FAIL — the retry reuses the cached (old) credentials, so the second
login/request pair never happens as expected (or the test's new
`get_calls`/`refresh=True` assertion has nothing to check against yet).

- [ ] **Step 3: Implement in `backend/connectors/estateably/connector.py`**

Change `_credentials(self) -> dict` to `_credentials(self, *, refresh: bool = False) -> dict[str, Any]: return self._secrets.get(self._secret_name, refresh=refresh)`.
Change `_login(self) -> str` to `_login(self, *, refresh_secret: bool = False) -> str`,
passing `refresh=refresh_secret` into its `self._credentials(...)` call.
In `_get_response`'s existing 401 handler, change `self._login()` to
`self._login(refresh_secret=True)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest backend/connectors/estateably/tests/test_connector.py -v`
Expected: PASS, full file green

- [ ] **Step 5: Repeat Steps 1-4 for Graph**

Same shape: `_credentials(self, *, refresh: bool = False)`;
`_fetch_token(self, *, refresh_secret: bool = False)` passes `refresh=refresh_secret`
into its `self._credentials(...)` call; `_ensure_token(self, *, force: bool = False)`
calls `self._fetch_token(refresh_secret=force)` instead of `self._fetch_token()`.
Test name: `test_401_after_reconnect_uses_fresh_secret_not_cache` in
`backend/connectors/graph/tests/test_connector.py`, same shape as Estateably's.

- [ ] **Step 6: Run full suite**

Run: `python -m pytest backend/connectors/estateably/ backend/connectors/graph/ -v`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add backend/connectors/estateably/connector.py backend/connectors/graph/connector.py \
        backend/connectors/estateably/tests/test_connector.py backend/connectors/graph/tests/test_connector.py
git commit -m "fix: re-read Secrets Manager on auth retry for Estateably and Graph"
```

---

### Task 3: Single-connector live-check helper

**Files:**
- Modify: `backend/harness.py`
- Test: `backend/tests/test_harness.py` (create if it doesn't exist — check first)

**Interfaces:**
- Consumes: `backend.harness._default_probes(secrets) -> dict[str, Callable[[], Any]]` (existing, private).
- Produces: `backend.harness.run_check(name: str, secrets: SecretsClient | None = None) -> dict[str, Any]`
  — Tasks 4-7 call this right after a successful credential save. Returns the
  same per-source shape `run_checks` produces for one source:
  `{"ok": bool, "detail": str}`. Raises `ValueError` for an unknown `name`.

- [ ] **Step 1: Write the failing test**

```python
def test_run_check_returns_single_source_result(monkeypatch):
    def fake_probe():
        return None
    monkeypatch.setattr(harness, "_default_probes", lambda secrets: {"estateably": fake_probe})
    result = harness.run_check("estateably", secrets=object())
    assert result == {"ok": True, "detail": "ok"}

def test_run_check_unknown_connector_raises():
    with pytest.raises(ValueError):
        harness.run_check("not-a-connector", secrets=object())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest backend/tests/test_harness.py -k run_check -v`
Expected: FAIL with `AttributeError: module 'backend.harness' has no attribute 'run_check'`

- [ ] **Step 3: Implement `run_check` in `backend/harness.py`**

```python
def run_check(name: str, secrets: SecretsClient | None = None) -> dict[str, Any]:
    secrets = secrets or SecretsClient()
    probes = _default_probes(secrets)
    if name not in probes:
        raise ValueError(f"unknown connector {name!r}")
    return run_checks({name: probes[name]}, secrets=secrets)["sources"][name]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest backend/tests/test_harness.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/harness.py backend/tests/test_harness.py
git commit -m "feat: add run_check for validating a single connector on demand"
```

---

### Task 4: Connector credential-write service module + Estateably/Graph routes

**Files:**
- Create: `backend/api/connectors_admin.py`
- Test: `backend/api/tests/test_connectors_admin.py`
- Modify: `backend/api/handlers.py` (routes + `Deps.secrets`)
- Test: `backend/api/tests/test_handlers.py`

**Interfaces:**
- Consumes: `services.secrets.SecretsClient.get(name, *, refresh=False)` /
  `.put(name, secret)`; `backend.harness.run_check(name, secrets)` (Task 3);
  `backend.api.handlers._require_admin` (Task 1).
- Produces: `backend.api.connectors_admin.ConnectorValidationError(Exception)`;
  `connect_estateably(secrets: SecretsClient, payload: dict[str, Any]) -> dict[str, Any]`;
  `connect_graph(secrets: SecretsClient, payload: dict[str, Any]) -> dict[str, Any]`
  (both return `run_check`'s `{"ok": bool, "detail": str}` on success, raise
  `ConnectorValidationError` on a bad credential). `backend.api.handlers.Deps.secrets: SecretsClient | None = None`
  (new, trailing field — Task 1-3's `Deps(...)` positional call sites are
  unaffected; `_build_deps` passes it explicitly).

- [ ] **Step 1: Write the failing tests for `connectors_admin.py`**

```python
class _FakeSecrets:
    def __init__(self):
        self.stored: dict[str, dict] = {}
    def get(self, name, *, refresh=False):
        return self.stored[name]
    def put(self, name, secret):
        self.stored[name] = secret

@responses.activate
def test_connect_estateably_stores_and_validates_on_success(monkeypatch):
    responses.add(responses.POST, "https://app.estateably.com/api/v1/auth/login",
                   json={"data": {"attributes": {"token": "t"}}}, status=200)
    monkeypatch.setattr(connectors_admin, "run_check", lambda name, secrets: {"ok": True, "detail": "ok"})
    secrets = _FakeSecrets()
    result = connectors_admin.connect_estateably(secrets, {"username": "u", "password": "p"})
    assert result == {"ok": True, "detail": "ok"}
    assert secrets.stored["aphrodite/estateably"] == {"username": "u", "password": "p"}

@responses.activate
def test_connect_estateably_bad_credentials_stores_nothing():
    responses.add(responses.POST, "https://app.estateably.com/api/v1/auth/login",
                   json={}, status=401)
    secrets = _FakeSecrets()
    with pytest.raises(connectors_admin.ConnectorValidationError):
        connectors_admin.connect_estateably(secrets, {"username": "u", "password": "wrong"})
    assert secrets.stored == {}

def test_connect_estateably_missing_fields_rejected():
    secrets = _FakeSecrets()
    with pytest.raises(connectors_admin.ConnectorValidationError):
        connectors_admin.connect_estateably(secrets, {"username": "", "password": ""})
    assert secrets.stored == {}

@responses.activate
def test_connect_estateably_reconnect_with_same_credentials_succeeds_again(monkeypatch):
    # Review Focus: resubmitting valid creds for an already-healthy connector
    # (a "reconnect" with no real problem) must not crash or behave differently.
    responses.add(responses.POST, "https://app.estateably.com/api/v1/auth/login",
                   json={"data": {"attributes": {"token": "t1"}}}, status=200)
    responses.add(responses.POST, "https://app.estateably.com/api/v1/auth/login",
                   json={"data": {"attributes": {"token": "t2"}}}, status=200)
    monkeypatch.setattr(connectors_admin, "run_check", lambda name, secrets: {"ok": True, "detail": "ok"})
    secrets = _FakeSecrets()
    body = {"username": "u", "password": "p"}
    first = connectors_admin.connect_estateably(secrets, body)
    second = connectors_admin.connect_estateably(secrets, body)
    assert first == second == {"ok": True, "detail": "ok"}
```

(Graph tests mirror these three exactly — `test_connect_graph_stores_and_validates_on_success`,
`test_connect_graph_bad_credentials_stores_nothing`,
`test_connect_graph_missing_fields_rejected` — mocking the
`https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token` POST
instead, with `{"tenant_id": "t", "client_id": "c", "client_secret": "s"}`
as the payload.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest backend/api/tests/test_connectors_admin.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.api.connectors_admin'`

- [ ] **Step 3: Implement `backend/api/connectors_admin.py`**

Define `ConnectorValidationError(Exception)`. A private `_StaticSecrets`
helper class (`get(self, name, *, refresh=False)` always returns one fixed
dict; no `put`) backs a throwaway connector instance used only to prove the
submitted credentials work before anything is persisted.

`connect_estateably`: strip/require `username`/`password` (optional
`base_url`); build the candidate secret dict; construct
`EstateablyConnector(_StaticSecrets(...))` and call its `_login()` — catch
any exception and re-raise as `ConnectorValidationError` with the original
message; on success, `secrets.put("aphrodite/estateably", candidate)` then
`return run_check("estateably", secrets)` (import `run_check` from
`backend.harness`).

`connect_graph`: same shape, requiring `tenant_id`/`client_id`/`client_secret`,
validating via a throwaway `GraphConnector(...)._fetch_token()`, storing under
`"aphrodite/graph"`, then `run_check("graph", secrets)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest backend/api/tests/test_connectors_admin.py -v`
Expected: PASS, 6/6

- [ ] **Step 5: Add `Deps.secrets` and wire the two routes in `handlers.py`**

Add `secrets: SecretsClient | None = None` as the last field of `Deps`
(import `SecretsClient` from `services.secrets` at module level — it's
currently only imported inside `_build_deps`). In `_build_deps`, the local
`secrets = SecretsClient(...)` variable already exists; pass it through to
the returned `Deps(...)` call as `secrets=secrets` (keyword, since the
constructor call is positional today — switch that one call to pass this
new field by keyword rather than renumbering 16 positional args).

In `handler()`, add, importing `connectors_admin` at module level:

```python
if method == "POST" and path.endswith("/connectors/estateably"):
    forbidden = _require_admin(event)
    if forbidden:
        return forbidden
    try:
        result = connectors_admin.connect_estateably(deps.secrets, _body(event))
    except connectors_admin.ConnectorValidationError as exc:
        return _response(400, {"error": str(exc)})
    return _response(200, result)
```

Same shape for `POST /connectors/graph` → `connectors_admin.connect_graph`.

- [ ] **Step 6: Write the failing handler-level tests**

```python
def test_connect_estateably_route_requires_admin(monkeypatch):
    # non-admin event, any body
    resp = handlers.handler(api_event("POST", "/connectors/estateably", body={}))
    assert resp["statusCode"] == 403

def test_connect_estateably_route_success(monkeypatch):
    monkeypatch.setattr(
        handlers.connectors_admin, "connect_estateably",
        lambda secrets, body: {"ok": True, "detail": "ok"},
    )
    resp = handlers.handler(admin_api_event("POST", "/connectors/estateably", body={"username": "u", "password": "p"}))
    assert resp["statusCode"] == 200
```

(Add an `admin_api_event` test helper alongside the existing `api_event` one,
identical except its `requestContext.authorizer.jwt.claims` includes
`"cognito:groups": '["admins"]'`. Mirror both tests for `/connectors/graph`.)

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m pytest backend/api/tests/ -v`
Expected: all pass

- [ ] **Step 8: Commit**

```bash
git add backend/api/connectors_admin.py backend/api/tests/test_connectors_admin.py \
        backend/api/handlers.py backend/api/tests/test_handlers.py
git commit -m "feat: add admin-gated Estateably and Graph connector routes"
```

---

### Task 5: Clio connector routes (out-of-band)

**Files:**
- Modify: `backend/api/connectors_admin.py`
- Test: `backend/api/tests/test_connectors_admin.py`
- Modify: `backend/api/handlers.py`
- Test: `backend/api/tests/test_handlers.py`

**Interfaces:**
- Consumes: `backend.connectors.clio.oauth_bootstrap.build_authorize_url`,
  `exchange_code` (existing, Phase-3-design-doc-identified as already
  reusable); `backend.api.connectors_admin.ConnectorValidationError` (Task 4).
- Produces: `connectors_admin.clio_authorize_url(payload: dict[str, Any]) -> dict[str, Any]`
  (`{"url": str, "state": str}`, no Secrets Manager write);
  `connectors_admin.clio_exchange(secrets: SecretsClient, payload: dict[str, Any]) -> dict[str, Any]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_clio_authorize_url_builds_oob_link():
    result = connectors_admin.clio_authorize_url({"client_id": "abc"})
    assert result["url"].startswith("https://app.clio.com/oauth/authorize?")
    assert "state" in result

def test_clio_authorize_url_missing_client_id_rejected():
    with pytest.raises(connectors_admin.ConnectorValidationError):
        connectors_admin.clio_authorize_url({})

@responses.activate
def test_clio_exchange_stores_and_validates_on_success():
    responses.add(responses.POST, "https://app.clio.com/oauth/token",
                   json={"refresh_token": "rt", "access_token": "at"}, status=200)
    secrets = _FakeSecrets()
    result = connectors_admin.clio_exchange(secrets, {"client_id": "c", "client_secret": "s", "code": "code123"})
    assert secrets.stored["aphrodite/clio"]["refresh_token"] == "rt"
    assert result == {"ok": True, "detail": "ok"}  # via monkeypatched run_check, as in Task 4

@responses.activate
def test_clio_exchange_failure_stores_nothing():
    responses.add(responses.POST, "https://app.clio.com/oauth/token", json={}, status=400)
    secrets = _FakeSecrets()
    with pytest.raises(connectors_admin.ConnectorValidationError):
        connectors_admin.clio_exchange(secrets, {"client_id": "c", "client_secret": "s", "code": "bad"})
    assert secrets.stored == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest backend/api/tests/test_connectors_admin.py -k clio -v`
Expected: FAIL — `AttributeError: module 'backend.api.connectors_admin' has no attribute 'clio_authorize_url'`

- [ ] **Step 3: Implement in `backend/api/connectors_admin.py`**

`clio_authorize_url(payload)`: require `client_id`; call
`backend.connectors.clio.oauth_bootstrap.build_authorize_url(client_id)`
(default `redirect_uri`/`base_url` — base_url override is an optional
`payload.get("base_url")` passthrough for non-US pods); return
`{"url": url, "state": state}`.

`clio_exchange(secrets, payload)`: require `client_id`/`client_secret`/`code`
(optional `base_url`); call `exchange_code(...)`, catching `HttpError` and
re-raising as `ConnectorValidationError`; on success build
`{"client_id":..., "client_secret":..., "refresh_token": payload["refresh_token"], "base_url": ...}`
(omit `base_url` key when not supplied, matching the existing
`scripts/clio_oauth_bootstrap.py` stored shape), `secrets.put("aphrodite/clio", ...)`,
`return run_check("clio", secrets)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest backend/api/tests/test_connectors_admin.py -v`
Expected: PASS

- [ ] **Step 5: Wire `POST /connectors/clio/authorize-url` and `POST /connectors/clio/exchange` in `handlers.py`**

Same admin-gated shape as Task 4 Step 5. `clio_authorize_url` never touches
`deps.secrets` (no admin-store side effect) so it can run before `deps` is
even needed for that purpose — still gate it with `_require_admin` (Global
Constraint: only admins see/generate the authorize link).

- [ ] **Step 6: Write and run the failing-then-passing handler tests**

Test names: `test_clio_authorize_url_route_requires_admin`,
`test_clio_authorize_url_route_success`, `test_clio_exchange_route_requires_admin`,
`test_clio_exchange_route_success` — same shape as Task 4 Step 6/7.

Run: `python -m pytest backend/api/tests/ -v`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add backend/api/connectors_admin.py backend/api/tests/test_connectors_admin.py \
        backend/api/handlers.py backend/api/tests/test_handlers.py
git commit -m "feat: add admin-gated Clio out-of-band connector routes"
```

---

### Task 6: QuickBooks connector routes (hosted redirect)

**Files:**
- Modify: `backend/api/connectors_admin.py`
- Test: `backend/api/tests/test_connectors_admin.py`
- Modify: `backend/api/handlers.py`
- Test: `backend/api/tests/test_handlers.py`

**Interfaces:**
- Consumes: `backend.connectors.quickbooks.oauth_bootstrap.build_authorize_url`,
  `exchange_code`; `connectors_admin.ConnectorValidationError` (Task 4).
- Produces: `connectors_admin.quickbooks_start(secrets, payload: dict[str, Any], *, redirect_uri: str) -> dict[str, Any]`
  (`{"url": str}`); `connectors_admin.quickbooks_callback(secrets, query: dict[str, Any], *, redirect_uri: str, app_base_url: str) -> str`
  (always returns a redirect URL string — success or failure — never raises,
  since its only caller is a browser-facing GET with no way to render a JSON
  error).

- [ ] **Step 1: Write the failing tests**

```python
def test_quickbooks_start_stores_pending_secret_and_returns_url():
    secrets = _FakeSecrets()
    result = connectors_admin.quickbooks_start(
        secrets, {"client_id": "c", "client_secret": "s", "environment": "sandbox"},
        redirect_uri="https://api.example/connectors/quickbooks/callback",
    )
    assert result["url"].startswith("https://appcenter.intuit.com/connect/oauth2?")
    stored = secrets.stored["aphrodite/quickbooks"]
    assert stored["client_id"] == "c" and "_oauth_state" in stored

def test_quickbooks_start_rejects_missing_fields():
    secrets = _FakeSecrets()
    with pytest.raises(connectors_admin.ConnectorValidationError):
        connectors_admin.quickbooks_start(secrets, {}, redirect_uri="https://x/callback")
    assert secrets.stored == {}

def test_quickbooks_start_rejects_bad_environment():
    secrets = _FakeSecrets()
    with pytest.raises(connectors_admin.ConnectorValidationError):
        connectors_admin.quickbooks_start(
            secrets, {"client_id": "c", "client_secret": "s", "environment": "prod"},
            redirect_uri="https://x/callback",
        )
    assert secrets.stored == {}

@responses.activate
def test_quickbooks_callback_matching_state_completes_exchange():
    secrets = _FakeSecrets()
    secrets.stored["aphrodite/quickbooks"] = {
        "client_id": "c", "client_secret": "s", "environment": "sandbox", "_oauth_state": "abc",
    }
    responses.add(responses.POST, "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
                   json={"refresh_token": "rt", "access_token": "at"}, status=200)
    url = connectors_admin.quickbooks_callback(
        secrets, {"code": "code1", "realmId": "r1", "state": "abc"},
        redirect_uri="https://api.example/connectors/quickbooks/callback",
        app_base_url="https://app.example",
    )
    assert url == "https://app.example/?setup=quickbooks-connected"
    stored = secrets.stored["aphrodite/quickbooks"]
    assert stored["refresh_token"] == "rt" and stored["realm_id"] == "r1" and "_oauth_state" not in stored

def test_quickbooks_callback_mismatched_state_does_not_complete():
    secrets = _FakeSecrets()
    secrets.stored["aphrodite/quickbooks"] = {
        "client_id": "c", "client_secret": "s", "environment": "sandbox", "_oauth_state": "abc",
    }
    url = connectors_admin.quickbooks_callback(
        secrets, {"code": "code1", "realmId": "r1", "state": "WRONG"},
        redirect_uri="https://api.example/connectors/quickbooks/callback",
        app_base_url="https://app.example",
    )
    assert url == "https://app.example/?setup=quickbooks-error"
    assert "refresh_token" not in secrets.stored["aphrodite/quickbooks"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest backend/api/tests/test_connectors_admin.py -k quickbooks -v`
Expected: FAIL — `AttributeError: module 'backend.api.connectors_admin' has no attribute 'quickbooks_start'`

- [ ] **Step 3: Implement in `backend/api/connectors_admin.py`**

`quickbooks_start`: strip/require `client_id`/`client_secret`; `environment`
defaults to `"sandbox"`, must be `"sandbox"` or `"production"` (else
`ConnectorValidationError`); generate `state = secrets_module.token_urlsafe(24)`
(`import secrets as _pysecrets`, aliased to avoid shadowing the `secrets:
SecretsClient` parameter); `secrets.put("aphrodite/quickbooks", {"client_id":
..., "client_secret": ..., "environment": ..., "_oauth_state": state})`
(a fresh `start` always overwrites — same control serves reconnect, per the
design); build the URL via `build_authorize_url(client_id, redirect_uri=redirect_uri, state=state)`;
return `{"url": url}`.

`quickbooks_callback`: wrap the whole body in `try/except Exception`,
returning `f"{app_base_url}/?setup=quickbooks-error"` on any failure
(missing `code`/`state`, a state mismatch, or `exchange_code` raising).
On the success path: `stored = secrets.get("aphrodite/quickbooks", refresh=True)`;
require `query.get("state") == stored.get("_oauth_state")` and `query.get("code")`
truthy (raise internally, caught by the wrapping `try`, if not); call
`exchange_code(stored["client_id"], stored["client_secret"], query["code"], redirect_uri=redirect_uri)`;
pop `_oauth_state`, set `refresh_token`/`realm_id` (from `query["realmId"]`),
`secrets.put(...)`, `run_check("quickbooks", secrets)` (best-effort — don't
let a health-check hiccup turn a real success into the error redirect; wrap
just that call in its own `try/except Exception: pass`), return
`f"{app_base_url}/?setup=quickbooks-connected"`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest backend/api/tests/test_connectors_admin.py -v`
Expected: PASS

- [ ] **Step 5: Wire the routes in `handlers.py`**

`POST /connectors/quickbooks/start` — admin-gated, same shape as prior
tasks, reading `redirect_uri` from `os.environ["APHRODITE_QUICKBOOKS_REDIRECT_URI"]`.

`GET /connectors/quickbooks/callback` — **not** admin-gated (it's hit by
Intuit's browser redirect with no Authorization header at all); dispatch it
in `handler()` reading `event.get("queryStringParameters") or {}`, call
`connectors_admin.quickbooks_callback(deps.secrets, query, redirect_uri=..., app_base_url=os.environ["APHRODITE_APP_BASE_URL"])`,
and return `{"statusCode": 302, "headers": {"Location": url}}` (not
`_response`, which always sets a JSON content type).

- [ ] **Step 6: Write and run the failing-then-passing handler tests**

```python
def test_quickbooks_start_route_requires_admin():
    resp = handlers.handler(api_event("POST", "/connectors/quickbooks/start", body={}))
    assert resp["statusCode"] == 403

def test_quickbooks_callback_route_redirects_without_admin_header(monkeypatch):
    monkeypatch.setattr(handlers.connectors_admin, "quickbooks_callback",
                         lambda *a, **k: "https://app.example/?setup=quickbooks-connected")
    event = api_event("GET", "/connectors/quickbooks/callback")
    event["queryStringParameters"] = {"code": "c", "realmId": "r", "state": "s"}
    resp = handlers.handler(event)
    assert resp["statusCode"] == 302
    assert resp["headers"]["Location"] == "https://app.example/?setup=quickbooks-connected"
```

Run: `python -m pytest backend/api/tests/ -v`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add backend/api/connectors_admin.py backend/api/tests/test_connectors_admin.py \
        backend/api/handlers.py backend/api/tests/test_handlers.py
git commit -m "feat: add QuickBooks connector start/callback routes"
```

---

### Task 7: Threshold-based alerting in the health store

**Files:**
- Modify: `backend/api/integrations.py`
- Test: `backend/api/tests/test_integrations.py`
- Modify: `backend/api/handlers.py` (`_build_deps`, `_run_health_probe`)
- Test: `backend/api/tests/test_handlers.py`

**Interfaces:**
- Produces: `IntegrationHealthStore.__init__(self, table, client=None, clock=time.time, on_alert: Callable[[str, str], None] | None = None)`
  (new keyword-only field, default `None` — existing call sites unaffected).

- [ ] **Step 1: Write the failing tests**

```python
def test_alert_fires_at_threshold_not_before():
    calls = []
    store = IntegrationHealthStore("t", client=fake_ddb_client(), on_alert=lambda name, detail: calls.append((name, detail)))
    store.record_result("clio", ok=False, detail="boom")  # 1st failure
    assert calls == []
    store.record_result("clio", ok=False, detail="boom")  # 2nd failure (ALERT_THRESHOLD)
    assert calls == [("clio", "boom")]

def test_alert_not_resent_on_subsequent_failures():
    calls = []
    store = IntegrationHealthStore("t", client=fake_ddb_client(), on_alert=lambda name, detail: calls.append((name, detail)))
    for _ in range(4):
        store.record_result("clio", ok=False, detail="boom")
    assert len(calls) == 1

def test_alert_not_fired_on_recovery():
    calls = []
    store = IntegrationHealthStore("t", client=fake_ddb_client(), on_alert=lambda name, detail: calls.append((name, detail)))
    store.record_result("clio", ok=False, detail="boom")
    store.record_result("clio", ok=False, detail="boom")
    store.record_result("clio", ok=True)
    assert len(calls) == 1  # only the original alert, not a second one on recovery

def test_no_on_alert_configured_is_a_noop():
    store = IntegrationHealthStore("t", client=fake_ddb_client())  # on_alert omitted
    store.record_result("clio", ok=False, detail="boom")
    store.record_result("clio", ok=False, detail="boom")  # must not raise

def test_repeated_healthy_results_never_fire_alert():
    # Review Focus: re-submitting good credentials for an already-healthy
    # connector (repeated ok=True) must not crash or spuriously alert.
    calls = []
    store = IntegrationHealthStore("t", client=fake_ddb_client(), on_alert=lambda name, detail: calls.append((name, detail)))
    store.record_result("clio", ok=True)
    store.record_result("clio", ok=True)
    store.record_result("clio", ok=True)
    assert calls == []
```

(Reuse this file's existing fake DynamoDB client helper — check the file for
its name before writing `fake_ddb_client()` literally.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest backend/api/tests/test_integrations.py -k alert -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'on_alert'`

- [ ] **Step 3: Implement in `backend/api/integrations.py`**

Add `on_alert: Callable[[str, str], None] | None = None` as a dataclass
field (with `field(default=None, init=False, repr=False)`-style private
backing not needed — it can be a plain `init=True` field like `clock`). In
`record_result`, after computing `consecutive` and before the existing
`_log_transition` call, add:

```python
if not ok and consecutive == ALERT_THRESHOLD and self.on_alert:
    self.on_alert(integration, detail or "ok")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest backend/api/tests/test_integrations.py -v`
Expected: PASS

- [ ] **Step 5: Wire SNS publish in `handlers.py`**

In `_build_deps`, build `on_alert` only when `APHRODITE_INTEGRATIONS_ALERT_TOPIC_ARN`
is set (mirroring the existing `quarterly`/`aml_trigger` "absent ⇒ no-op"
pattern): a closure that calls `boto3.client("sns", region_name=REGION).publish(TopicArn=..., Subject=f"{name} connector is down", Message=detail)`
inside its own `try/except Exception: logger.warning(...)` (an alert failure
must not break the health probe, same discipline as `record_result` itself).
Pass it to `IntegrationHealthStore(health_table, on_alert=on_alert)`.

- [ ] **Step 6: Write and run the failing-then-passing test**

```python
def test_build_deps_wires_sns_alert_when_topic_configured(monkeypatch):
    monkeypatch.setenv("APHRODITE_INTEGRATIONS_ALERT_TOPIC_ARN", "arn:aws:sns:...:topic")
    # ... construct via the test module's existing _build_deps mocking pattern;
    # assert deps.integrations.on_alert is not None
```

Run: `python -m pytest backend/api/tests/ -v`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add backend/api/integrations.py backend/api/tests/test_integrations.py \
        backend/api/handlers.py backend/api/tests/test_handlers.py
git commit -m "feat: fire an SNS alert at the health store's existing failure threshold"
```

---

### Task 8: Terraform wiring

**Files:**
- Modify: `terraform/modules/search_api/main.tf`
- Modify: `terraform/modules/search_api/variables.tf`
- Modify: `terraform/modules/search_api/outputs.tf`
- Modify: `terraform/main.tf`
- Modify: `terraform/variables.tf`

**Interfaces:**
- Consumes: Task 1's `admins` Cognito group (reference only — no Terraform
  dependency needed, since group membership is assigned out-of-band); Task 7's
  `APHRODITE_INTEGRATIONS_ALERT_TOPIC_ARN` env var name.

- [ ] **Step 1: Add the six new routes to `search_api/main.tf`**

Five join the existing `aws_apigatewayv2_route.routes` `for_each` list:
`"POST /connectors/estateably"`, `"POST /connectors/graph"`,
`"POST /connectors/clio/authorize-url"`, `"POST /connectors/clio/exchange"`,
`"POST /connectors/quickbooks/start"`. The sixth, `GET /connectors/quickbooks/callback`,
is a **separate** `aws_apigatewayv2_route` resource (not in that `for_each`,
since it needs `authorization_type = "NONE"`):

```hcl
resource "aws_apigatewayv2_route" "quickbooks_callback" {
  api_id             = aws_apigatewayv2_api.search.id
  route_key          = "GET /connectors/quickbooks/callback"
  target             = "integrations/${aws_apigatewayv2_integration.search.id}"
  authorization_type = "NONE"
}
```

- [ ] **Step 2: Add the new Lambda env vars**

In `aws_lambda_function.search`'s `environment.variables`, add:
`APHRODITE_QUICKBOOKS_REDIRECT_URI = "${aws_apigatewayv2_api.search.api_endpoint}/connectors/quickbooks/callback"`,
`APHRODITE_APP_BASE_URL = var.app_base_url`,
`APHRODITE_INTEGRATIONS_ALERT_TOPIC_ARN = aws_sns_topic.integrations_alert.arn`.

- [ ] **Step 3: Add `var.app_base_url` to `search_api/variables.tf`**

```hcl
variable "app_base_url" {
  type        = string
  description = "The deployed SPA's origin (e.g. CloudFront domain), used to redirect the browser back after the QuickBooks OAuth callback."
}
```

Wire it through from `terraform/variables.tf` (a new root variable of the
same name and description) via `terraform/main.tf`'s `module "search_api"`
block, the same passthrough pattern Phase 1 used for `org_name` etc.

- [ ] **Step 4: Add the SNS topic + email subscriptions**

In `search_api/main.tf`, mirroring `quarterly_report/main.tf`'s existing
`aws_sns_topic.notify` / `aws_sns_topic_subscription.emails` pair exactly:

```hcl
resource "aws_sns_topic" "integrations_alert" {
  name = "aphrodite-integrations-alert"
}

resource "aws_sns_topic_subscription" "integrations_alert_emails" {
  for_each  = toset(var.integrations_alert_emails)
  topic_arn = aws_sns_topic.integrations_alert.arn
  protocol  = "email"
  endpoint  = each.value
}
```

Add `variable "integrations_alert_emails" { type = list(string); default = [] }`
to `search_api/variables.tf`, and the matching root variable +
`terraform/main.tf` passthrough, same pattern as `app_base_url`.

- [ ] **Step 5: Add IAM permissions**

In `data.aws_iam_policy_document.search` (`search_api/main.tf`): add
`sns:Publish` scoped to `[aws_sns_topic.integrations_alert.arn]`; extend the
existing `WriteQuickBooksToken`/`WriteClioToken` write-scoped statements'
resource lists (or add two new statements, `WriteEstateablyToken`/
`WriteGraphToken`) covering `var.estateably_secret_arn`/`var.graph_secret_arn`
— two new variables in `search_api/variables.tf`, same shape as the existing
`quickbooks_secret_arn`/`clio_secret_arn`, wired from `terraform/main.tf`.

- [ ] **Step 6: Verify**

Run: `terraform -chdir=terraform fmt -check -recursive && terraform -chdir=terraform validate`
Expected: `Success! The configuration is valid` (same pre-existing warnings only)

- [ ] **Step 7: Commit**

```bash
git add terraform/modules/search_api/ terraform/main.tf terraform/variables.tf
git commit -m "feat: wire connector-onboarding routes, SNS alert topic, and IAM for Terraform"
```

---

### Task 9: Frontend — actionable Setup screen

**Files:**
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/components/IntegrationsPanel.tsx`
- Create: `frontend/src/components/ConnectorCard.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/main.tsx`

**Interfaces:**
- Consumes: the five new routes from Tasks 4-6 (`POST /connectors/estateably`,
  `POST /connectors/graph`, `POST /connectors/clio/authorize-url`,
  `POST /connectors/clio/exchange`, `POST /connectors/quickbooks/start`).
- Produces: `App` gains a new `isAdmin?: boolean` prop, threaded to
  `IntegrationsPanel`.

- [ ] **Step 1: Add API functions to `frontend/src/api.ts`**

```typescript
export async function connectEstateably(base: string, body: {username: string; password: string; base_url?: string}, token?: string): Promise<{ok: boolean; detail: string}>
export async function connectGraph(base: string, body: {tenant_id: string; client_id: string; client_secret: string}, token?: string): Promise<{ok: boolean; detail: string}>
export async function clioAuthorizeUrl(base: string, body: {client_id: string}, token?: string): Promise<{url: string; state: string}>
export async function clioExchange(base: string, body: {client_id: string; client_secret: string; code: string}, token?: string): Promise<{ok: boolean; detail: string}>
export async function quickbooksStart(base: string, body: {client_id: string; client_secret: string; environment: string}, token?: string): Promise<{url: string}>
```

Each follows the existing `authHeaders(token)` + `fetch` + `if (!res.ok) throw`
pattern already used by every other POST in this file (e.g.
`generateReport`), parsing a `{error: string}` body on failure the same way
`updateAmlSchedule` does.

- [ ] **Step 2: Write a failing test for one of them**

(Check whether `frontend/src/api.ts` has any existing test file first — if
none exists for this file, skip a dedicated unit test here and rely on the
manual browser verification in Step 6, noting that in the commit message;
if one exists, follow its existing mocking pattern for one representative
function, e.g. `connectEstateably`, asserting the request body/method/URL.)

- [ ] **Step 3: Extend `IntegrationStatus`-adjacent types in `frontend/src/types.ts`**

No new type is strictly required (the five functions above return plain
inline-typed objects) — skip unless the existing file's convention is to
name every API response shape, in which case add
`ConnectorActionResult { ok: boolean; detail: string }`.

- [ ] **Step 4: Build `ConnectorCard.tsx` and wire it into `IntegrationsPanel.tsx`**

`ConnectorCard` renders one of four action bodies by `integration` id:
- `"quickbooks"`: a button calling `quickbooksStart`, then
  `window.location.href = result.url`.
- `"clio"`: a button calling `clioAuthorizeUrl` and showing the returned
  `url` as a link plus a code-paste `<input>` + submit calling `clioExchange`
  with the same `client_id` and the pasted code (the admin re-enters
  `client_id`/`client_secret` in a small form above both buttons, since both
  Clio calls need them and nothing before this point has them).
- `"estateably"`: a form (`username`, `password`, optional `base_url`)
  calling `connectEstateably`.
- `"graph"`: a form (`tenant_id`, `client_id`, `client_secret`) calling
  `connectGraph`.

Every card's action, on success, calls the existing `refresh()` from
`useIntegrationsHealth()` so status updates immediately rather than waiting
for the next 60s poll. `IntegrationsPanel` renders a `ConnectorCard` per row
only when `isAdmin` is true (a prop threaded in); non-admins see exactly
today's read-only table (no behavior change for them).

- [ ] **Step 5: Thread `isAdmin` from `main.tsx` through `App.tsx`**

In `main.tsx`'s `AuthedApp`, compute:

```typescript
const groups = auth.user?.profile?.["cognito:groups"];
const isAdmin = Array.isArray(groups) && groups.includes("admins");
```

Pass `isAdmin={isAdmin}` to `<App>`; add `isAdmin?: boolean` to `App`'s props
and pass it through to `<IntegrationsPanel>` (mirroring how `token`/`mock`
already thread through `App.tsx`).

- [ ] **Step 6: Manual verification**

Per this project's working approach (frontend changes get browser-verified,
not just type-checked): run the dev server in mock mode, open the
Integrations tab with `isAdmin` forced `true` (temporarily hardcode it or
add a `?admin=1` query-param override for local testing only, removed before
commit), and confirm each of the four card types renders and its button/form
is clickable without a console error. Mock-mode `fetch` calls will fail
(no real backend) — confirm the resulting error is shown inline on the card,
not an unhandled exception/blank screen.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/api.ts frontend/src/types.ts frontend/src/components/IntegrationsPanel.tsx \
        frontend/src/components/ConnectorCard.tsx frontend/src/App.tsx frontend/src/main.tsx
git commit -m "feat: make the Integrations tab actionable for admins (connect/reconnect)"
```

---

### Task 10: Documentation

**Files:**
- Modify: `backend/README.md`
- Modify: `frontend/README.md` (check whether it exists first; if not, skip and note why in the commit)

**Interfaces:**
- Consumes: the final route paths and env var names from Tasks 4-8.

- [ ] **Step 1: Document the new routes and env vars in `backend/README.md`**

Add a short section listing the five new routes, the `admins` Cognito group
requirement, and the three new env vars (`APHRODITE_QUICKBOOKS_REDIRECT_URI`,
`APHRODITE_APP_BASE_URL`, `APHRODITE_INTEGRATIONS_ALERT_TOPIC_ARN`), same
table style as the existing "Search configuration" section.

- [ ] **Step 2: Verify**

Run: `grep -rn "APHRODITE_APP_BASE_URL\|admins" backend/README.md`
Expected: at least one match each

- [ ] **Step 3: Commit**

```bash
git add backend/README.md
git commit -m "docs: document connector onboarding routes and config"
```
