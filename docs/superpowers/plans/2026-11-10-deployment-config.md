# Deployment Config Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an admin view and edit the 13 Phase 1 business-config fields from a new
Setup-screen tab, with changes taking effect immediately — including for a request already
being served by a warm search_api container — with no `terraform apply`.

**Architecture:** A single SSM Parameter Store JSON blob (`/aphrodite/config/deployment`),
read/written through a new `ConfigClient` (mirroring the existing `SecretsClient`), behind
two new admin-gated API routes. Every consuming Lambda's existing `os.environ.get(...)`
call sites gain a "check the stored config first" line in front of them — untouched
otherwise. search_api additionally stops baking its 3 directly-used fields into
constructor-time objects, resolving them fresh per request instead.

**Tech Stack:** Python 3.13 (backend/Lambda), boto3 (SSM), TypeScript/React (frontend),
Terraform.

**Spec:** `docs/superpowers/specs/2026-11-10-phase3-completion-design.md`

## Global Constraints

- Exactly 13 fields, names unchanged from the existing Terraform variables (spec §5) —
  never renamed, never a 14th added.
- One SSM parameter, `/aphrodite/config/deployment`. No Terraform-managed
  `aws_ssm_parameter` resource — created lazily by its first `put_parameter(Overwrite=True)`
  (spec §8).
- `PUT /config` validation failure is a flat `{"error": "<field>: <message>"}` — the same
  shape `_response(400, {"error": str(exc)})` already uses everywhere else (spec
  "Decisions from builder-check").
- The Deployment Config tab is visible to everyone; `isAdmin` gates the edit controls only,
  matching the Integrations tab's existing pattern (spec "Decisions from builder-check").
- `framework.py` / `quality.py` / `scoping.py` / `render_docx.py` / `classification.py`
  never change — they keep taking plain parameters (spec §3, §7).
- Only search_api needs per-request (not per-cold-start) resolution, for exactly 3 fields:
  `org_name`, `org_office_address`, `excluded_sharepoint_folders` (spec §7). The three
  scheduled Lambdas (quarterly_report, aml_export, the SharePoint crawler) resolve once per
  invocation — no further restructuring there.
- All 13 fields become env vars on search_api's Lambda too, purely so `GET /config` can
  report an accurate `"source": "default"` value for every field (spec "Decisions from
  builder-check").

## Review Focus

- A field explicitly configured to an empty value (`[]` for a list field, `""` for a
  string) must be honored as "configured to empty," not treated as falsy-equals-
  unconfigured and silently fall back to the env var/default.
- A config save must be visible to the very next search_api request on an already-warm
  container — not just after that container's next cold start.
- `PUT /config` with an `internal_matter_number_pattern` that doesn't compile as a regex
  must be rejected before it's written to SSM — never silently stored and crash the next
  AML export run.
- The very first `GET /config` on a deployment that has never saved anything (the SSM
  parameter doesn't exist yet) must return every field as `"source": "default"`, not a 500
  on `ParameterNotFound`.
- A `PUT /config` that sets one field must not erase other, previously-configured fields —
  the store is read-merge-write, never read-replace-write.

---

### Task 1: `ConfigClient`

**Files:**
- Create: `services/config.py`
- Test: `services/tests/test_config.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `ConfigClient(client=None, parameter_name="/aphrodite/config/deployment")`,
  `.get(*, refresh: bool = False) -> dict[str, Any]`, `.put(values: dict[str, Any]) -> None`
  — later tasks construct one per Lambda and call these two methods by these exact names.

- [ ] **Step 1: Write the failing tests**

```python
# services/tests/test_config.py
import json
import pytest
from botocore.exceptions import ClientError
from services.config import ConfigClient


class _FakeSsm:
    def __init__(self):
        self.params = {}

    def get_parameter(self, *, Name):
        if Name not in self.params:
            raise ClientError(
                {"Error": {"Code": "ParameterNotFound", "Message": "not found"}},
                "GetParameter",
            )
        return {"Parameter": {"Value": self.params[Name]}}

    def put_parameter(self, *, Name, Value, Type, Overwrite):
        self.params[Name] = Value


def test_get_returns_empty_dict_when_parameter_never_created():
    client = ConfigClient(client=_FakeSsm())
    assert client.get() == {}


def test_put_then_get_round_trips():
    ssm = _FakeSsm()
    client = ConfigClient(client=ssm)
    client.put({"org_name": "Acme Trust"})
    assert client.get(refresh=True) == {"org_name": "Acme Trust"}


def test_put_merges_rather_than_replaces():
    ssm = _FakeSsm()
    client = ConfigClient(client=ssm)
    client.put({"org_name": "Acme Trust"})
    client.put({"org_office_address": "1 Main St"})
    assert client.get(refresh=True) == {
        "org_name": "Acme Trust",
        "org_office_address": "1 Main St",
    }


def test_put_with_none_value_clears_that_key():
    ssm = _FakeSsm()
    client = ConfigClient(client=ssm)
    client.put({"org_name": "Acme Trust", "org_office_address": "1 Main St"})
    client.put({"org_name": None})
    assert client.get(refresh=True) == {"org_office_address": "1 Main St"}


def test_get_caches_until_refresh():
    ssm = _FakeSsm()
    client = ConfigClient(client=ssm)
    client.put({"org_name": "Acme Trust"})
    ssm.params["/aphrodite/config/deployment"] = json.dumps({"org_name": "tampered"})
    assert client.get() == {"org_name": "Acme Trust"}  # cached, not re-read
    assert client.get(refresh=True) == {"org_name": "tampered"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.config'`.

- [ ] **Step 3: Implement `ConfigClient` in `services/config.py`**

Mirror `services/secrets.py::SecretsClient`'s shape (injectable `client`, in-memory cache).
`get()` calls `self._client.get_parameter(Name=self._parameter_name)`, catching
`ClientError` with `error.response["Error"]["Code"] == "ParameterNotFound"` and returning
`{}` in that case (re-raising any other `ClientError`); otherwise `json.loads` the
`Parameter.Value`. `put(values)` reads the current stored dict (bypassing the cache — a
fresh read), merges `values` onto it (a `None` value deletes that key from the merged
dict), writes the merged dict back via `put_parameter(Name=..., Value=json.dumps(merged),
Type="String", Overwrite=True)`, and updates the cache to the merged dict.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/tests/test_config.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add services/config.py services/tests/test_config.py
git commit -m "feat: add ConfigClient for the deployment config SSM store"
```

---

### Task 2: Config schema + `GET`/`PUT` business logic

**Files:**
- Create: `backend/api/config_admin.py`
- Test: `backend/api/tests/test_config_admin.py`

**Interfaces:**
- Consumes: Task 1's `ConfigClient.get()`/`.put()`.
- Produces: `get_config(config: ConfigClient | None) -> dict[str, dict[str, Any]]` (the
  `GET /config` response body's `"fields"` value) and `put_config(config: ConfigClient,
  updates: dict[str, Any]) -> None` (raises `ConfigValidationError` on an invalid field) —
  Task 3's routes call these two by these exact names.

- [ ] **Step 1: Write the failing tests**

```python
# backend/api/tests/test_config_admin.py
import pytest
from backend.api.config_admin import ConfigValidationError, get_config, put_config
from services.config import ConfigClient


class _FakeSsm:
    def __init__(self):
        self.params = {}

    def get_parameter(self, *, Name):
        from botocore.exceptions import ClientError
        if Name not in self.params:
            raise ClientError(
                {"Error": {"Code": "ParameterNotFound", "Message": "not found"}},
                "GetParameter",
            )
        return {"Parameter": {"Value": self.params[Name]}}

    def put_parameter(self, *, Name, Value, Type, Overwrite):
        self.params[Name] = Value


def test_get_config_reports_default_for_every_field_when_nothing_configured(monkeypatch):
    monkeypatch.setenv("APHRODITE_ORG_NAME", "Heritage Trust")
    client = ConfigClient(client=_FakeSsm())
    fields = get_config(client)
    assert fields["org_name"] == {"value": "Heritage Trust", "source": "default"}
    assert fields["quarterly_qualifying_practice_areas"] == {"value": [], "source": "default"}


def test_get_config_reports_configured_source_after_a_put(monkeypatch):
    monkeypatch.setenv("APHRODITE_ORG_NAME", "Heritage Trust")
    client = ConfigClient(client=_FakeSsm())
    put_config(client, {"org_name": "Acme Trust"})
    assert get_config(client)["org_name"] == {"value": "Acme Trust", "source": "configured"}


def test_get_config_distinguishes_configured_empty_list_from_unconfigured():
    client = ConfigClient(client=_FakeSsm())
    put_config(client, {"quarterly_qualifying_practice_areas": []})
    field = get_config(client)["quarterly_qualifying_practice_areas"]
    assert field == {"value": [], "source": "configured"}


def test_put_config_rejects_a_pattern_that_does_not_compile():
    client = ConfigClient(client=_FakeSsm())
    with pytest.raises(ConfigValidationError, match="internal_matter_number_pattern"):
        put_config(client, {"internal_matter_number_pattern": "["})
    assert get_config(client)["internal_matter_number_pattern"]["source"] == "default"


def test_put_config_rejects_a_blank_entry_in_a_list_field():
    client = ConfigClient(client=_FakeSsm())
    with pytest.raises(ConfigValidationError, match="own_company_names"):
        put_config(client, {"own_company_names": ["Acme", "  "]})


def test_put_config_null_clears_a_field_back_to_default(monkeypatch):
    monkeypatch.setenv("APHRODITE_ORG_NAME", "Heritage Trust")
    client = ConfigClient(client=_FakeSsm())
    put_config(client, {"org_name": "Acme Trust"})
    put_config(client, {"org_name": None})
    assert get_config(client)["org_name"] == {"value": "Heritage Trust", "source": "default"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest backend/api/tests/test_config_admin.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.api.config_admin'`.

- [ ] **Step 3: Implement in `backend/api/config_admin.py`**

Define `ConfigValidationError(Exception)`. Define the 13-field schema as a module-level
structure: for each field, its name, type (`str` or `list[str]`), its env var name, and its
in-code default (copy the exact default values from the table in spec §5). Add a `validate`
per type: a string field has no extra validation except `internal_matter_number_pattern`,
which must be non-empty and must compile via `re.compile`; a list field rejects any entry
that is blank/whitespace-only after stripping (mirrors the existing Terraform `validation`
blocks word-for-word).

`get_config(config)`: call `config.get()` once; for each of the 13 fields, if its name is a
key in the returned dict (checked with `in`, never `if value:` — Review Focus item 1),
report `{"value": stored[name], "source": "configured"}`; otherwise report
`{"value": os.environ.get(env_var, default), "source": "default"}`.

`put_config(config, updates)`: for each key in `updates`, look it up in the schema (an
unknown key is itself a `ConfigValidationError`); if the value is not `None`, validate it
per the field's rule above, raising `ConfigValidationError(f"{name}: <reason>")` on failure
— validate **every** key before writing any of them, so a partially-invalid request writes
nothing. Once every key passes, call `config.put(updates)` once with the whole dict
(`None` values pass through for `ConfigClient.put` to clear, per Task 1).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest backend/api/tests/test_config_admin.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add backend/api/config_admin.py backend/api/tests/test_config_admin.py
git commit -m "feat: add the deployment-config field schema and GET/PUT business logic"
```

---

### Task 3: Wire `GET /config` / `PUT /config` into `handlers.py`

**Files:**
- Modify: `backend/api/handlers.py`
- Test: `backend/api/tests/test_handlers.py`

**Interfaces:**
- Consumes: Task 2's `get_config`/`put_config`/`ConfigValidationError`.
- Produces: `Deps.config: ConfigClient | None = None` (new field, last in the dataclass) —
  Task 4 reads it by this exact name.

- [ ] **Step 1: Write the failing tests**

```python
def test_get_config_route_returns_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.config import ConfigClient

    deps, _ = install_deps(monkeypatch)
    deps.config = ConfigClient(client=_FakeSsmForConfig())
    resp = handlers.handler(api_event("GET", "/config"))
    assert resp["statusCode"] == 200
    assert "org_name" in json.loads(resp["body"])["fields"]


def test_put_config_route_requires_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.config import ConfigClient

    deps, _ = install_deps(monkeypatch)
    deps.config = ConfigClient(client=_FakeSsmForConfig())
    resp = handlers.handler(api_event("PUT", "/config", body={"org_name": "Acme Trust"}))
    assert resp["statusCode"] == 403


def test_put_config_route_success(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.config import ConfigClient

    deps, _ = install_deps(monkeypatch)
    deps.config = ConfigClient(client=_FakeSsmForConfig())
    resp = handlers.handler(
        admin_api_event("PUT", "/config", body={"org_name": "Acme Trust"})
    )
    assert resp["statusCode"] == 200
    assert deps.config.get(refresh=True)["org_name"] == "Acme Trust"


def test_put_config_route_rejects_invalid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.config import ConfigClient

    deps, _ = install_deps(monkeypatch)
    deps.config = ConfigClient(client=_FakeSsmForConfig())
    resp = handlers.handler(
        admin_api_event(
            "PUT", "/config", body={"internal_matter_number_pattern": "["}
        )
    )
    assert resp["statusCode"] == 400
    assert "internal_matter_number_pattern" in json.loads(resp["body"])["error"]
```

Add a module-level `class _FakeSsmForConfig` near the top of the test file (same shape as
`_FakeSsm` in Task 1/2's tests — `get_parameter`/`put_parameter` backed by a dict).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest backend/api/tests/test_handlers.py -k "config_route" -v`
Expected: FAIL — 404 (no `/config` route exists yet); `Deps` has no `config` field.

- [ ] **Step 3: Implement in `backend/api/handlers.py`**

Add `config: ConfigClient | None = None` as the last field of `Deps`. In `_build_deps()`,
construct `config = ConfigClient(boto3.client("ssm", region_name=REGION))`
(same style as the existing `secrets = SecretsClient(...)` line) and pass
`config=config` into the final `Deps(...)` call.

Add two routes, admin-gated the same way `/connectors/*` is (`forbidden =
_require_admin(event); if forbidden: return forbidden` before the body):

```python
if method == "GET" and path.endswith("/config"):
    return _response(200, {"fields": config_admin.get_config(deps.config)})

if method == "PUT" and path.endswith("/config"):
    forbidden = _require_admin(event)
    if forbidden:
        return forbidden
    try:
        config_admin.put_config(deps.config, _body(event))
    except config_admin.ConfigValidationError as exc:
        return _response(400, {"error": str(exc)})
    return _response(200, {"fields": config_admin.get_config(deps.config)})
```

`GET /config` is deliberately **not** admin-gated — Global Constraints says the tab (and
its values) is visible to everyone; only the edit (`PUT`) is admin-only. Import
`config_admin` at module scope alongside the other `backend.api.*` imports.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest backend/api/tests/test_handlers.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add backend/api/handlers.py backend/api/tests/test_handlers.py
git commit -m "feat: add GET/PUT /config routes backed by the deployment config store"
```

---

### Task 4: Per-request config resolution in search_api

**Files:**
- Modify: `backend/search/synthesize.py`
- Modify: `backend/search/orchestrator.py`
- Modify: `backend/api/pipeline.py`
- Modify: `backend/api/handlers.py`
- Test: `backend/search/tests/test_synthesize.py`
- Test: `backend/search/tests/test_orchestrator.py`
- Test: `backend/api/tests/test_pipeline.py`
- Test: `backend/api/tests/test_handlers.py`

**Interfaces:**
- Consumes: Task 3's `Deps.config`.
- Produces: `MatterSynthesizer.synthesize(..., org_name: str | None = None, office_address:
  str | None = None)`; `DossierBuilder.build(..., excluded_sharepoint_folders:
  frozenset[str] | None = None)`; `run_search(..., org_name: str | None = None,
  office_address: str | None = None, excluded_sharepoint_folders: frozenset[str] | None =
  None)` — no later task consumes these; this is the leaf of the chain.

- [ ] **Step 1: Write the failing test for `MatterSynthesizer`**

```python
def test_synthesize_override_replaces_the_constructed_framework():
    synth = MatterSynthesizer(FakeLlm(), org_name="Aphrodite", office_address="545 Clyde")
    synth.synthesize(MatterDossier("x", None), "q", org_name="Acme", office_address="1 Main St")
    assert "Acme" in fake_llm_last_cache_system(synth)  # adapt to this file's existing fixture style
    assert "Aphrodite" not in fake_llm_last_cache_system(synth)


def test_synthesize_without_override_uses_the_constructed_framework():
    synth = MatterSynthesizer(FakeLlm(), org_name="Aphrodite", office_address="545 Clyde")
    synth.synthesize(MatterDossier("x", None), "q")
    assert "Aphrodite" in fake_llm_last_cache_system(synth)
```

(Adapt to `test_synthesize.py`'s existing `FakeLlm`/assertion-helper pattern — check the
file for how it currently asserts on `cache_system` before writing this literally.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest backend/search/tests/test_synthesize.py -k override -v`
Expected: FAIL — `synthesize()` doesn't accept `org_name`/`office_address`.

- [ ] **Step 3: Implement the override in `MatterSynthesizer.synthesize`**

Add `org_name: str | None = None, office_address: str | None = None` as new keyword-only
parameters. When either is given, compute `framework = render_framework(org_name or
self._org_name, office_address or self._office_address)` for this call only and pass it as
`cache_system=framework` instead of `self._framework`; when neither is given, behavior is
unchanged (`self._framework`). This requires `__init__` to also store `self._org_name`/
`self._office_address` (currently only used to build `self._framework`, not retained) so a
call that overrides only one of the two still has the other's constructed value to fall
back to.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest backend/search/tests/test_synthesize.py -v`
Expected: all pass.

- [ ] **Step 5: Write the failing test for `DossierBuilder`**

```python
def test_build_override_replaces_the_constructed_exclusion_set():
    builder = DossierBuilder(graph=FakeGraph(), excluded_sharepoint_folders=frozenset({"hr"}))
    cand = MatterCandidate(name="Smith", refs=[SourceRef("clio", "1", "Smith")], matter_type="estate")
    builder.build(cand, correspondence_term="smith", excluded_sharepoint_folders=frozenset({"finance"}))
    # Adapt to this file's existing pattern for asserting which folders _collect_sharepoint excluded
    # (e.g. a FakeGraph that records the `exclude` kwarg it was called with).
```

(Check `test_orchestrator.py` for its existing SharePoint-collection test fixtures before
writing this literally — follow its established pattern for faking the Graph connector and
observing what `_collect_sharepoint` passed as `exclude`.)

- [ ] **Step 6: Run test to verify it fails**

Run: `pytest backend/search/tests/test_orchestrator.py -k override -v`
Expected: FAIL — `build()` doesn't accept `excluded_sharepoint_folders`.

- [ ] **Step 7: Implement the override in `DossierBuilder.build`**

Add `excluded_sharepoint_folders: frozenset[str] | None = None` as a new keyword-only
parameter on `build()`. Thread it into `_collect_sharepoint`'s call as `exclude=
excluded_sharepoint_folders if excluded_sharepoint_folders is not None else
self._excluded_sharepoint_folders`.

- [ ] **Step 8: Run test to verify it passes**

Run: `pytest backend/search/tests/test_orchestrator.py -v`
Expected: all pass.

- [ ] **Step 9: Write the failing test for `run_search` threading the overrides through**

```python
def test_run_search_threads_config_overrides_into_build_and_synthesize():
    calls = {}

    class RecordingBuilder(FakeBuilder):
        def build(self, c, **kwargs):
            calls["build"] = kwargs
            return super().build(c, **kwargs)

    class RecordingSynth(FakeSynth):
        def synthesize(self, d, q, **kwargs):
            calls["synthesize"] = kwargs
            return super().synthesize(d, q, **kwargs)

    run_search(
        "q",
        resolver=FakeResolver(_resolution([_candidate()])),
        dossier_builder=RecordingBuilder(MatterDossier("x", None)),
        synthesizer=RecordingSynth(_synth()),
        excluded_sharepoint_folders=frozenset({"legal"}),
        org_name="Acme Trust",
        office_address="1 Main St",
    )
    assert calls["build"]["excluded_sharepoint_folders"] == frozenset({"legal"})
    assert calls["synthesize"]["org_name"] == "Acme Trust"
    assert calls["synthesize"]["office_address"] == "1 Main St"
```

(`FakeBuilder`/`FakeSynth` already exist in this file and accept `**_` today — this test
subclasses them to also record the kwargs they receive, rather than widening the originals.)

- [ ] **Step 10: Run test to verify it fails**

Run: `pytest backend/api/tests/test_pipeline.py -k config_overrides -v`
Expected: FAIL — `run_search()` doesn't accept the three new parameters.

- [ ] **Step 11: Implement in `backend/api/pipeline.py`**

Add `org_name: str | None = None, office_address: str | None = None,
excluded_sharepoint_folders: frozenset[str] | None = None` as new keyword-only parameters
on `run_search`. Pass `excluded_sharepoint_folders=excluded_sharepoint_folders` into the
existing `dossier_builder.build(candidate, correspondence_term=term)` call, and
`org_name=org_name, office_address=office_address` into the existing
`synthesizer.synthesize(dossier, query, remaining_ms=remaining_ms)` call.

- [ ] **Step 12: Run test to verify it passes**

Run: `pytest backend/api/tests/test_pipeline.py -v`
Expected: all pass.

- [ ] **Step 13: Write the failing test proving a saved config change is visible to the
  very next request on an already-warm deps cache (Review Focus)**

```python
def test_config_change_is_visible_to_the_next_request_without_a_new_deps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.config import ConfigClient

    deps, _ = install_deps(monkeypatch)  # one Deps instance, simulating a warm container
    deps.config = ConfigClient(client=_FakeSsmForConfig())
    seen_org_names: list[str | None] = []
    real_synthesize = deps.synthesizer.synthesize

    def recording_synthesize(d, q, **kwargs):
        seen_org_names.append(kwargs.get("org_name"))
        return real_synthesize(d, q, **kwargs)

    monkeypatch.setattr(deps.synthesizer, "synthesize", recording_synthesize)

    handlers.handler(
        {"worker": True, "job_id": "j1", "query": "Smith", "actor": {}}
    )
    deps.config.put({"org_name": "Acme Trust"})
    handlers.handler(
        {"worker": True, "job_id": "j2", "query": "Smith", "actor": {}}
    )

    assert seen_org_names[0] != seen_org_names[1]
    assert seen_org_names[1] == "Acme Trust"
```

(Reuses `_FakeSsmForConfig` from Task 3's tests and `install_deps`'s existing `FakeSynth`
instance — monkeypatches its bound `synthesize` method to record kwargs rather than
widening the fixture class itself.)

- [ ] **Step 14: Run test to verify it fails**

Run: `pytest backend/api/tests/test_handlers.py -k config_change_is_visible -v`
Expected: FAIL — `_run_worker` doesn't yet read `deps.config` or pass overrides into
`run_search`.

- [ ] **Step 15: Implement in `backend/api/handlers.py`'s `_run_worker`**

Before the `run_search(...)` call, resolve the 3 fields the same way `get_config` does in
Task 2 (read `deps.config.get()` once if `deps.config is not None`, else `{}`; for each of
`org_name`, `org_office_address`, `excluded_sharepoint_folders`, use the stored value if the
key is present — checked with `in`, not truthiness — else `os.environ.get(env_var,
default)`). Pass the three resolved values into `run_search(...)` as the new
`org_name=`, `office_address=`, `excluded_sharepoint_folders=` keyword arguments
(`excluded_sharepoint_folders` needs the same JSON-or-comma parsing
`_excluded_folders_from_env` already does when it comes from the env-var path — reuse that
existing function unchanged for the fallback case; only the stored-config case bypasses it,
since the stored value is already a typed list).

- [ ] **Step 16: Run test to verify it passes, then the whole suite**

Run: `pytest backend/search/tests/test_synthesize.py backend/search/tests/test_orchestrator.py backend/api/tests/test_pipeline.py backend/api/tests/test_handlers.py -v`
Expected: all pass.

- [ ] **Step 17: Commit**

```bash
git add backend/search/synthesize.py backend/search/orchestrator.py backend/api/pipeline.py backend/api/handlers.py backend/search/tests/test_synthesize.py backend/search/tests/test_orchestrator.py backend/api/tests/test_pipeline.py backend/api/tests/test_handlers.py
git commit -m "feat: resolve deployment config per-request in search_api, not at cold start"
```

---

### Task 5: Scheduled Lambdas read the deployment config

**Files:**
- Modify: `backend/lambdas/quarterly_report/handler.py`
- Modify: `backend/lambdas/aml_export/handler.py`
- Modify: `backend/lambdas/sharepoint_crawler/handler.py`
- Test: `backend/lambdas/quarterly_report/tests/test_handler.py`
- Test: `backend/lambdas/aml_export/tests/test_handler.py`
- Test: `backend/lambdas/sharepoint_crawler/tests/test_handler.py`

**Interfaces:**
- Consumes: Task 1's `ConfigClient`.
- Produces: nothing — this is a leaf task, three independent small changes with the same
  shape.

- [ ] **Step 1: Write the failing test for `quarterly_report`**

```python
def test_letterhead_from_env_prefers_configured_value(monkeypatch):
    monkeypatch.setenv("APHRODITE_LETTERHEAD_NAME", "Env Name")
    config = ConfigClient(client=_FakeSsm())
    config.put({"letterhead_name": "Configured Name"})
    letterhead = _letterhead_from_env(config)
    assert letterhead.name == "Configured Name"


def test_letterhead_from_env_falls_back_when_config_client_is_none(monkeypatch):
    monkeypatch.setenv("APHRODITE_LETTERHEAD_NAME", "Env Name")
    assert _letterhead_from_env(None).name == "Env Name"
```

(Check this test file's existing fakes for an SSM double it already has, or copy `_FakeSsm`
from Task 1's tests — follow this file's established import/fixture style.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest backend/lambdas/quarterly_report/tests/test_handler.py -k letterhead -v`
Expected: FAIL — `_letterhead_from_env()` takes no arguments today.

- [ ] **Step 3: Implement in `backend/lambdas/quarterly_report/handler.py`**

Add `config: ConfigClient | None` as `_letterhead_from_env`'s one parameter. For each of its
4 fields, use `config.get()["<name>"]` when `config is not None and "<name>" in
config.get()`, else fall through to the existing `os.environ.get(...)` expression,
unchanged. Do the same for `_qualifying_from_env(config)` and the inline
`exclusion_field=os.environ.get("APHRODITE_QUARTERLY_EXCLUSION_FIELD", "").strip()` line in
`_build_deps` (give it the same "check config first" treatment inline, or extract a
one-line helper — implementer's choice). In `_build_deps()`, construct `config =
ConfigClient(boto3.client("ssm", region_name=REGION))` the same way `secrets` is already
constructed, and pass it into the three call sites above.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest backend/lambdas/quarterly_report/tests/test_handler.py -v`
Expected: all pass.

- [ ] **Step 5: Write the failing test for `aml_export`**

```python
def test_org_config_from_env_prefers_configured_own_company_names(monkeypatch):
    monkeypatch.setenv("APHRODITE_OWN_COMPANY_NAMES", '["Env Co"]')
    config = ConfigClient(client=_FakeSsm())
    config.put({"own_company_names": ["Configured Co"]})
    assert _org_config_from_env(config).own_company_names == ("Configured Co",)
```

- [ ] **Step 6: Run test to verify it fails**

Run: `pytest backend/lambdas/aml_export/tests/test_handler.py -k org_config -v`
Expected: FAIL — `_org_config_from_env()` takes no arguments today.

- [ ] **Step 7: Implement in `backend/lambdas/aml_export/handler.py`**

Same treatment as Task 5 Step 3: `_org_config_from_env(config: ConfigClient | None)`,
checking `config.get()` for `own_company_names`, `internal_title_keywords`,
`internal_matter_number_pattern` before each existing env-var expression. Construct
`config` in `_build_deps()` the same way.

- [ ] **Step 8: Run test to verify it passes**

Run: `pytest backend/lambdas/aml_export/tests/test_handler.py -v`
Expected: all pass.

- [ ] **Step 9: Write the failing test for the SharePoint crawler**

```python
def test_excluded_folders_from_env_prefers_configured_value(monkeypatch):
    config = ConfigClient(client=_FakeSsm())
    config.put({"excluded_sharepoint_folders": ["legal"]})
    assert _excluded_folders_from_env(config) == frozenset({"legal"})


def test_sharepoint_sites_prefers_configured_value(monkeypatch):
    monkeypatch.setenv("APHRODITE_SHAREPOINT_SITES", '["https://env.example"]')
    config = ConfigClient(client=_FakeSsm())
    config.put({"sharepoint_sites": ["https://configured.example"]})
    assert _sharepoint_sites_from_env(config) == ["https://configured.example"]
```

- [ ] **Step 10: Run test to verify it fails**

Run: `pytest backend/lambdas/sharepoint_crawler/tests/test_handler.py -k "excluded_folders or sharepoint_sites" -v`
Expected: FAIL — neither function exists with this signature yet (`sites` is read inline
today, not via a named function).

- [ ] **Step 11: Implement in `backend/lambdas/sharepoint_crawler/handler.py`**

Give `_excluded_folders_from_env` a `config: ConfigClient | None` parameter, same treatment
as above. Extract the inline `sites = json.loads(os.environ.get("APHRODITE_SHAREPOINT_SITES",
"[]"))` line into a new `_sharepoint_sites_from_env(config: ConfigClient | None) ->
list[str]` function with the same "check config first" shape, and call it from `handler()`
in place of the inline expression. Construct `config` in `handler()` the same way `secrets`
is already constructed there.

- [ ] **Step 12: Run test to verify it passes, then the whole suite**

Run: `pytest backend/lambdas/ -v`
Expected: all pass.

- [ ] **Step 13: Commit**

```bash
git add backend/lambdas/quarterly_report/handler.py backend/lambdas/aml_export/handler.py backend/lambdas/sharepoint_crawler/handler.py backend/lambdas/quarterly_report/tests/test_handler.py backend/lambdas/aml_export/tests/test_handler.py backend/lambdas/sharepoint_crawler/tests/test_handler.py
git commit -m "feat: the three scheduled Lambdas read the deployment config store"
```

---

### Task 6: Terraform — env vars + IAM

**Files:**
- Modify: `terraform/main.tf`
- Modify: `terraform/modules/search_api/main.tf`
- Modify: `terraform/modules/search_api/variables.tf`
- Modify: `terraform/modules/quarterly_report/main.tf` (find the file holding its IAM
  policy document and Lambda environment block — same module that already sets
  `APHRODITE_LETTERHEAD_NAME` etc.)
- Modify: `terraform/modules/aml_export/main.tf` (same, for its own env vars/IAM)
- Modify: `terraform/modules/sharepoint_kb/main.tf` (the crawler's module)

**Interfaces:**
- Consumes: nothing from earlier tasks (pure Terraform; Tasks 1–5 already work against
  `boto3.client("ssm", region_name=REGION)` with no Terraform-specific assumption beyond the
  IAM grant existing).
- Produces: nothing later tasks consume — this is the infrastructure leaf.

- [ ] **Step 1: Add the 10 missing variables + env vars to search_api's module**

In `terraform/modules/search_api/variables.tf`, add the 10 variables this module doesn't
have yet: `own_company_names`, `internal_title_keywords`, `internal_matter_number_pattern`,
`letterhead_name`, `letterhead_address`, `letterhead_signoff`, `letterhead_privacy_notice`,
`quarterly_qualifying_practice_areas`, `quarterly_exclusion_field`, `sharepoint_sites` — same
types as the root `terraform/variables.tf` declarations (copy the `type` from there). In
`terraform/modules/search_api/main.tf`'s `aws_lambda_function.search` environment block, add
the matching 10 `APHRODITE_*` env vars (list-typed ones via `jsonencode(var.x)`, matching
the existing `APHRODITE_EXCLUDED_SHAREPOINT_FOLDERS = jsonencode(var.excluded_sharepoint_folders)`
line's style). In root `terraform/main.tf`'s `module "search_api"` block, pass all 10 new
variables through from the existing root-level `var.x` (every one of the 13 root variables
already exists in `terraform/variables.tf` from Phase 1 — this task only wires the 10 that
aren't yet passed into *this* module).

- [ ] **Step 2: Add the SSM IAM grants**

In `terraform/modules/search_api/main.tf`'s `data.aws_iam_policy_document.search`, add two
statements (not conditional — config always exists, unlike Phase 2's per-connector toggles):

```hcl
statement {
  sid       = "ReadDeploymentConfig"
  actions   = ["ssm:GetParameter"]
  resources = ["arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/aphrodite/config/deployment"]
}
statement {
  sid       = "WriteDeploymentConfig"
  actions   = ["ssm:PutParameter"]
  resources = ["arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/aphrodite/config/deployment"]
}
```

In `terraform/modules/quarterly_report/main.tf`, `terraform/modules/aml_export/main.tf`, and
`terraform/modules/sharepoint_kb/main.tf`, add only the `ReadDeploymentConfig` statement
(same `resources` ARN construction — each module already has its own
`data.aws_caller_identity.current` + `var.aws_region`, confirmed by the builder-check) to
each module's own Lambda execution-role policy document.

- [ ] **Step 3: Verify**

Run: `terraform -chdir=terraform fmt -check -recursive && terraform -chdir=terraform validate`
Expected: `Success! The configuration is valid` (same pre-existing warnings only).

- [ ] **Step 4: Commit**

```bash
git add terraform/main.tf terraform/modules/search_api/ terraform/modules/quarterly_report/ terraform/modules/aml_export/ terraform/modules/sharepoint_kb/
git commit -m "feat: wire deployment-config env vars and SSM IAM grants in Terraform"
```

---

### Task 7: Frontend — Deployment Config tab

**Files:**
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/nav.ts`
- Modify: `frontend/src/App.tsx`
- Create: `frontend/src/components/DeploymentConfigPanel.tsx`

**Interfaces:**
- Consumes: Task 3's `GET`/`PUT /config` routes.
- Produces: nothing later tasks consume — this is the UI leaf.

- [ ] **Step 1: Add `fetchConfig`/`updateConfig` to `frontend/src/api.ts`**

Follow `fetchCapabilities`'s existing pattern (same `authHeaders`/`fetch` shape). Unlike
`fetchCapabilities`, these must **not** swallow a failure into a safe default — a config
read/write failure is something the admin needs to see, so let a non-OK response's `.error`
body throw, same as `connectEstateably`/`clioExchange`'s existing pattern elsewhere in this
file.

```typescript
export interface ConfigField { value: unknown; source: "configured" | "default" }
export interface ConfigResponse { fields: Record<string, ConfigField> }

export async function fetchConfig(base: string, token?: string): Promise<ConfigResponse> { ... }
export async function updateConfig(
  base: string,
  updates: Record<string, unknown>,
  token?: string,
): Promise<ConfigResponse> { ... }
```

- [ ] **Step 2: Add the tab to `frontend/src/nav.ts`**

Add `"deployment-config"` to the `TabId` union and a matching `ModuleTab` entry in `TABS`
(label "Deployment Config", `ready: true`, an icon in the same inline-SVG style as the
others).

- [ ] **Step 3: Build `DeploymentConfigPanel.tsx`**

Props: `{ apiBase: string; token?: string; isAdmin?: boolean }`. On mount, call
`fetchConfig`. Render one form section per consuming Lambda (Search / AML Export /
Quarterly Reports / SharePoint — group the 13 fields by the table in spec §5), each field
showing its current value, a "configured" vs "using default" badge from `field.source`, and
— only when `isAdmin` — an editable input plus a "Reset to default" button that calls
`updateConfig(apiBase, { [name]: null }, token)`. Reuse the existing `banner`/`ResultNotice`
conventions for save feedback and validation errors (surface a `PUT` failure's `.error`
message in a `banner-error`).

- [ ] **Step 4: Wire it into `App.tsx`**

Add a branch to the existing `activeTab === "..."` ternary chain, rendering
`<DeploymentConfigPanel apiBase={apiBase} token={token} isAdmin={isAdmin} />` when
`activeTab === "deployment-config"` — same shape as the existing `IntegrationsPanel` branch.

- [ ] **Step 5: Typecheck and build**

Run: `npx tsc -b` (in `frontend/`) and `npm run build`.
Expected: both clean.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api.ts frontend/src/nav.ts frontend/src/App.tsx frontend/src/components/DeploymentConfigPanel.tsx
git commit -m "feat: add the Deployment Config tab"
```
