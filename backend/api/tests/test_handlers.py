"""Unit tests for the Lambda handler routing + worker. Deps are faked; no AWS."""

from __future__ import annotations

import json
from typing import Any

import pytest

from backend.api import handlers
from backend.api.jobs import InMemoryJobStore
from backend.search import scoping


class _FakeSsmForConfig:
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


class FakeResolver:
    def __init__(self, result: Any) -> None:
        self._result = result

    def resolve(self, query: str, **_: Any) -> Any:
        return self._result


class Recorder:
    """Captures self-invoke payloads; can run them through the handler inline."""

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def __call__(self, payload: dict[str, Any]) -> None:
        self.payloads.append(payload)


def install_deps(monkeypatch: pytest.MonkeyPatch, **over: Any) -> tuple[handlers.Deps, Recorder]:
    from backend.search.orchestrator import MatterDossier
    from backend.search.resolve import MatterCandidate, ResolutionResult, SourceRef
    from backend.search.synthesize import SynthesisResult

    cand = MatterCandidate(
        name="Smith Estate",
        refs=[SourceRef("clio", "9", "Smith")],
        matter_type="estate",
        score=1.0,
    )
    resolution = over.get("resolution", ResolutionResult("q", "smith", "estate", [cand]))

    class FakeBuilder:
        def build(self, c: Any, **_: Any) -> MatterDossier:
            return MatterDossier("Smith Estate", "estate")

    class FakeSynth:
        def synthesize(self, d: Any, q: str, **_: Any) -> SynthesisResult:
            return SynthesisResult(
                sections={"current_stage": "early admin"},
                citations=[],
                invalid_citations=[],
                sources_reviewed=[],
                insufficient_info=False,
                model_id="m",
                input_tokens=1,
                output_tokens=1,
                raw_text="{}",
            )

    rec = Recorder()
    deps = handlers.Deps(
        resolver=FakeResolver(resolution),
        dossier_builder=FakeBuilder(),
        synthesizer=FakeSynth(),
        store=InMemoryJobStore(),
        self_invoke=rec,
    )
    monkeypatch.setattr(handlers, "_DEPS", deps)
    return deps, rec


def api_event(
    method: str,
    path: str,
    *,
    body: dict | None = None,
    params: dict | None = None,
    sub: str = "user-1",
) -> dict:
    return {
        "requestContext": {
            "http": {"method": method, "path": path},
            "authorizer": {"jwt": {"claims": {"sub": sub, "email": "a@b.ca"}}},
        },
        "body": json.dumps(body) if body is not None else None,
        "pathParameters": params or {},
    }


# -- _build_deps / modular flags ---------------------------------------------


def test_build_deps_skips_disabled_connectors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APHRODITE_JOBS_TABLE", "jobs")
    monkeypatch.setenv("APHRODITE_SELF_FUNCTION", "fn")
    monkeypatch.setenv("APHRODITE_ESTATEABLY_ENABLED", "false")
    deps = handlers._build_deps()
    assert "estateably" not in deps.connectors_enabled
    assert "clio" in deps.connectors_enabled  # unset env var defaults to enabled


def test_build_deps_all_connectors_enabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APHRODITE_JOBS_TABLE", "jobs")
    monkeypatch.setenv("APHRODITE_SELF_FUNCTION", "fn")
    deps = handlers._build_deps()
    assert deps.connectors_enabled == frozenset({"clio", "estateably", "quickbooks", "graph"})


def test_build_deps_reports_disabled_when_quarterly_function_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APHRODITE_JOBS_TABLE", "jobs")
    monkeypatch.setenv("APHRODITE_SELF_FUNCTION", "fn")
    monkeypatch.setenv("APHRODITE_OUTPUTS_BUCKET", "bucket")
    monkeypatch.delenv("APHRODITE_QUARTERLY_FUNCTION", raising=False)
    deps = handlers._build_deps()
    assert deps.reports is None  # outputs_bucket alone is no longer enough


def test_build_deps_disabling_graph_also_disables_sharepoint_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Important (I2 from the Phase 2 final review): graph_enabled is documented
    # as "Outlook/SharePoint" — disabling it must stop SharePoint documents from
    # still appearing in dossiers via the Bedrock Knowledge Base client.
    monkeypatch.setenv("APHRODITE_JOBS_TABLE", "jobs")
    monkeypatch.setenv("APHRODITE_SELF_FUNCTION", "fn")
    monkeypatch.setenv("APHRODITE_SHAREPOINT_KB_ID", "kb-123")
    monkeypatch.setenv("APHRODITE_GRAPH_ENABLED", "false")
    deps = handlers._build_deps()
    assert deps.dossier_builder._knowledge_base is None


def test_build_deps_screener_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APHRODITE_JOBS_TABLE", "jobs")
    monkeypatch.setenv("APHRODITE_SELF_FUNCTION", "fn")
    monkeypatch.setenv("APHRODITE_OUTPUTS_BUCKET", "bucket")
    monkeypatch.delenv("APHRODITE_SANCTION_SCREENER_ENABLED", raising=False)
    deps = handlers._build_deps()
    assert deps.screener is None


# -- routing ----------------------------------------------------------------


def test_resolve_route(monkeypatch: pytest.MonkeyPatch) -> None:
    install_deps(monkeypatch)
    resp = handlers.handler(api_event("POST", "/resolve", body={"query": "Smith estate"}))
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["candidates"][0]["name"] == "Smith Estate"


def test_search_creates_job_and_self_invokes(monkeypatch: pytest.MonkeyPatch) -> None:
    deps, rec = install_deps(monkeypatch)
    resp = handlers.handler(api_event("POST", "/search", body={"query": "Smith estate"}))
    assert resp["statusCode"] == 202
    job_id = json.loads(resp["body"])["job_id"]
    # job persisted as pending + worker dispatched with the actor
    assert deps.store.get(job_id)["status"] == "pending"
    assert rec.payloads[0]["worker"] is True
    assert rec.payloads[0]["actor"]["sub"] == "user-1"


def test_search_forwards_scope_to_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    _, rec = install_deps(monkeypatch)
    resp = handlers.handler(api_event("POST", "/search", body={"query": "Kober", "scope": "clio"}))
    assert resp["statusCode"] == 202
    assert rec.payloads[0]["scope"] == "clio"


def test_search_scope_defaults_to_all(monkeypatch: pytest.MonkeyPatch) -> None:
    _, rec = install_deps(monkeypatch)
    # No scope, and a junk scope, both normalize to "all".
    handlers.handler(api_event("POST", "/search", body={"query": "Kober"}))
    handlers.handler(api_event("POST", "/search", body={"query": "Kober", "scope": "bogus"}))
    assert rec.payloads[0]["scope"] == "all"
    assert rec.payloads[1]["scope"] == "all"


def test_search_forwards_selection_to_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    _, rec = install_deps(monkeypatch)
    selection = {
        "name": "Estate of Lee",
        "matter_type": "estate",
        "refs": [{"system": "clio", "id": "5"}],
    }
    resp = handlers.handler(
        api_event("POST", "/search", body={"query": "Estate of Lee", "selection": selection})
    )
    assert resp["statusCode"] == 202
    assert rec.payloads[0]["selection"] == selection


def test_connector_health_route_returns_source_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    deps, _ = install_deps(monkeypatch)
    deps.store.create("j", "q", {"sub": "user-1"})
    deps.store.complete("j", {"status": "ok", "dossier_errors": ["QuickBooks unavailable"]})
    resp = handlers.handler(api_event("GET", "/connector-health"))
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["source_errors"] == ["QuickBooks unavailable"]


def test_list_search_returns_only_callers_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    deps, _ = install_deps(monkeypatch)
    deps.store.create("mine-1", "yurkiw", {"sub": "user-1"})
    deps.store.create("theirs", "sparks", {"sub": "user-2"})
    resp = handlers.handler(api_event("GET", "/search"))
    assert resp["statusCode"] == 200
    ids = {s["job_id"] for s in json.loads(resp["body"])["searches"]}
    assert ids == {"mine-1"}  # user-2's job never surfaces


def test_get_job_rejects_non_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    deps, _ = install_deps(monkeypatch)
    deps.store.create("theirs", "q", {"sub": "user-2"})
    resp = handlers.handler(api_event("GET", "/search/theirs", params={"jobId": "theirs"}))
    assert resp["statusCode"] == 404  # 404 (not 403) — do not leak existence


def test_get_job_owner_can_read(monkeypatch: pytest.MonkeyPatch) -> None:
    deps, _ = install_deps(monkeypatch)
    deps.store.create("mine", "q", {"sub": "user-1"})
    resp = handlers.handler(api_event("GET", "/search/mine", params={"jobId": "mine"}))
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["job_id"] == "mine"


def test_delete_job_rejects_non_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    deps, _ = install_deps(monkeypatch)
    deps.store.create("theirs", "q", {"sub": "user-2"})
    resp = handlers.handler(api_event("DELETE", "/search/theirs", params={"jobId": "theirs"}))
    assert resp["statusCode"] == 404  # 404 (not 403) — do not leak existence
    assert deps.store.get("theirs") is not None  # untouched


def test_delete_job_owner_can_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    deps, _ = install_deps(monkeypatch)
    deps.store.create("mine", "q", {"sub": "user-1"})
    resp = handlers.handler(api_event("DELETE", "/search/mine", params={"jobId": "mine"}))
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["deleted"] == "mine"
    assert deps.store.get("mine") is None


def test_delete_job_missing_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    install_deps(monkeypatch)
    resp = handlers.handler(api_event("DELETE", "/search/nope", params={"jobId": "nope"}))
    assert resp["statusCode"] == 404


def test_search_then_worker_then_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    deps, rec = install_deps(monkeypatch)
    start = handlers.handler(api_event("POST", "/search", body={"query": "Smith estate"}))
    job_id = json.loads(start["body"])["job_id"]

    # Run the dispatched worker payload (as the async invoke would).
    handlers.handler(rec.payloads[0])

    poll = handlers.handler(api_event("GET", f"/search/{job_id}", params={"jobId": job_id}))
    body = json.loads(poll["body"])
    assert body["status"] == "done"
    assert body["result"]["answer"]["sections"]["current_stage"] == "early admin"


def test_missing_query_is_400(monkeypatch: pytest.MonkeyPatch) -> None:
    install_deps(monkeypatch)
    assert handlers.handler(api_event("POST", "/search", body={}))["statusCode"] == 400


def test_unknown_job_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    install_deps(monkeypatch)
    resp = handlers.handler(api_event("GET", "/search/nope", params={"jobId": "nope"}))
    assert resp["statusCode"] == 404


def test_unknown_route_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    install_deps(monkeypatch)
    assert handlers.handler(api_event("DELETE", "/whatever"))["statusCode"] == 404


# -- worker error handling --------------------------------------------------


def test_worker_records_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    deps, _ = install_deps(monkeypatch)

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("resolve exploded")

    deps.resolver.resolve = boom  # type: ignore[method-assign]
    deps.store.create("j1", "q", {"sub": "u"})
    out = handlers.handler({"worker": True, "job_id": "j1", "query": "q", "actor": None})
    assert out["ok"] is False
    assert deps.store.get("j1")["status"] == "error"
    assert "exploded" in deps.store.get("j1")["error"]


# -- AML admin (run now + monthly schedule) ---------------------------------


class FakeAmlTrigger:
    def __init__(self) -> None:
        self.ran = 0

    def run(self) -> None:
        self.ran += 1


class FakeAmlSchedule:
    def __init__(self) -> None:
        self.state = {"enabled": False, "day_of_month": 1, "hour": 0, "minute": 0, "timezone": "T"}

    def get(self) -> dict:
        return dict(self.state)

    def update(self, *, enabled: bool, day_of_month: int, hour: int, minute: int) -> dict:
        self.state = {
            "enabled": enabled,
            "day_of_month": day_of_month,
            "hour": hour,
            "minute": minute,
            "timezone": "T",
        }
        return dict(self.state)


def test_aml_generate_triggers(monkeypatch: pytest.MonkeyPatch) -> None:
    deps, _ = install_deps(monkeypatch)
    deps.aml_trigger = FakeAmlTrigger()
    resp = handlers.handler(api_event("POST", "/aml-exports/generate"))
    assert resp["statusCode"] == 202
    assert deps.aml_trigger.ran == 1


def test_aml_generate_503_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    install_deps(monkeypatch)  # aml_trigger defaults to None
    assert handlers.handler(api_event("POST", "/aml-exports/generate"))["statusCode"] == 503


def test_screener_delete_route(monkeypatch: pytest.MonkeyPatch) -> None:
    deps, _ = install_deps(monkeypatch)

    class FakeScreener:
        def __init__(self) -> None:
            self.deleted: str | None = None

        def delete(self, key: str) -> int:
            self.deleted = key
            return 2

    deps.screener = FakeScreener()  # type: ignore[assignment]
    ev = api_event("DELETE", "/screener/certificates")
    ev["queryStringParameters"] = {"key": "screener/2026-07/run/Certificate.pdf"}
    resp = handlers.handler(ev)
    assert resp["statusCode"] == 200
    assert deps.screener.deleted == "screener/2026-07/run/Certificate.pdf"
    assert json.loads(resp["body"])["removed"] == 2


# -- Activity Logs (chain-of-custody) ---------------------------------------


class FakeActivity:
    def __init__(self, items: list[dict] | None = None) -> None:
        self.items = items or []
        self.records: list[dict] = []

    def record(self, actor, action, summary, **fields) -> None:  # type: ignore[no-untyped-def]
        self.records.append({"actor": actor, "action": action, "summary": summary, **fields})

    def list(self, limit: int = 500) -> list[dict]:
        return self.items[:limit]


def test_activity_list_route(monkeypatch: pytest.MonkeyPatch) -> None:
    deps, _ = install_deps(monkeypatch)
    deps.activity = FakeActivity(  # type: ignore[assignment]
        [{"action": "download", "summary": "Downloaded a file"}]
    )
    resp = handlers.handler(api_event("GET", "/activity"))
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["activity"][0]["action"] == "download"


def test_activity_list_503_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    install_deps(monkeypatch)  # activity defaults to None
    assert handlers.handler(api_event("GET", "/activity"))["statusCode"] == 503


def test_action_records_activity_with_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    deps, _ = install_deps(monkeypatch)
    deps.aml_trigger = FakeAmlTrigger()
    deps.activity = FakeActivity()  # type: ignore[assignment]
    handlers.handler(api_event("POST", "/aml-exports/generate"))
    assert len(deps.activity.records) == 1
    entry = deps.activity.records[0]
    assert entry["action"] == "aml.run"
    assert entry["actor"]["email"] == "a@b.ca"


class FakeIntegrations:
    def __init__(self, snapshot: dict | None = None) -> None:
        self._snapshot = snapshot or {"integrations": [], "log": [], "alerting": False}
        self.recorded: list[dict] = []
        self.snapshot_enabled: frozenset[str] | None = "not called"  # type: ignore[assignment]

    def record_result(self, integration, *, ok, detail="", suggested_fix=""):  # type: ignore[no-untyped-def]
        self.recorded.append(
            {"integration": integration, "ok": ok, "detail": detail, "suggested_fix": suggested_fix}
        )

    def snapshot(self, *, log_limit: int = 50, enabled: frozenset[str] | None = None) -> dict:
        self.snapshot_enabled = enabled
        return self._snapshot


def test_integrations_route_returns_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    deps, _ = install_deps(monkeypatch)
    deps.integrations = FakeIntegrations(  # type: ignore[assignment]
        {
            "integrations": [{"integration": "clio", "ok": True, "consecutive_failures": 0}],
            "log": [],
            "alerting": False,
        }
    )
    resp = handlers.handler(api_event("GET", "/integrations"))
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["integrations"][0]["integration"] == "clio"


def test_integrations_route_503_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    install_deps(monkeypatch)  # integrations defaults to None
    assert handlers.handler(api_event("GET", "/integrations"))["statusCode"] == 503


def test_integrations_route_filters_snapshot_by_enabled_connectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deps, _ = install_deps(monkeypatch)
    deps.integrations = FakeIntegrations()  # type: ignore[assignment]
    deps.connectors_enabled = frozenset({"clio"})
    handlers.handler(api_event("GET", "/integrations"))
    assert deps.integrations.snapshot_enabled == frozenset({"clio"})


def test_health_probe_records_each_source_with_suggested_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.harness as harness_mod

    deps, _ = install_deps(monkeypatch)
    deps.integrations = FakeIntegrations()  # type: ignore[assignment]
    monkeypatch.setattr(
        harness_mod,
        "run_checks",
        lambda **_: {
            "ok": False,
            "sources": {
                "clio": {"ok": True, "detail": "ok"},
                "quickbooks": {
                    "ok": False,
                    "detail": "HttpError: QuickBooks token refresh failed: 400",
                },
            },
        },
    )
    # The probe path is not an API request — invoked with {"health_probe": true}.
    out = handlers.handler({"health_probe": True})
    assert out["ok"] is False
    assert out["checked"] == ["clio", "quickbooks"]
    recorded = {r["integration"]: r for r in deps.integrations.recorded}
    assert recorded["clio"]["ok"] is True and recorded["clio"]["suggested_fix"] == ""
    assert recorded["quickbooks"]["ok"] is False
    assert "re-authorize" in recorded["quickbooks"]["suggested_fix"].lower()


def test_health_probe_only_checks_enabled_connectors(monkeypatch: pytest.MonkeyPatch) -> None:
    # Important (C3 from the Phase 2 final review): probing a disabled
    # connector gets AccessDenied (its ReadSecrets grant is withdrawn) and
    # would otherwise be recorded as a false "down" / trigger a false alert.
    import backend.harness as harness_mod

    deps, _ = install_deps(monkeypatch)
    deps.integrations = FakeIntegrations()  # type: ignore[assignment]
    deps.connectors_enabled = frozenset({"clio"})
    captured: dict[str, Any] = {}

    def fake_run_checks(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"ok": True, "sources": {"clio": {"ok": True, "detail": "ok"}}}

    monkeypatch.setattr(harness_mod, "run_checks", fake_run_checks)
    handlers.handler({"health_probe": True})
    assert captured["enabled"] == frozenset({"clio"})


def test_aml_schedule_get_and_put(monkeypatch: pytest.MonkeyPatch) -> None:
    deps, _ = install_deps(monkeypatch)
    deps.aml_schedule = FakeAmlSchedule()

    got = handlers.handler(api_event("GET", "/aml-exports/schedule"))
    assert got["statusCode"] == 200
    assert json.loads(got["body"])["enabled"] is False

    put = handlers.handler(
        api_event(
            "PUT",
            "/aml-exports/schedule",
            body={"enabled": True, "day_of_month": 5, "hour": 9, "minute": 0},
        )
    )
    assert put["statusCode"] == 200
    body = json.loads(put["body"])
    assert body["enabled"] is True
    assert body["day_of_month"] == 5
    assert body["hour"] == 9


def test_excluded_folders_from_env_malformed_json_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APHRODITE_EXCLUDED_SHAREPOINT_FOLDERS", "{not valid json")
    assert handlers._excluded_folders_from_env() == scoping.DEFAULT_EXCLUDED_FOLDERS


def test_excluded_folders_from_env_empty_list_excludes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APHRODITE_EXCLUDED_SHAREPOINT_FOLDERS", "[]")
    assert handlers._excluded_folders_from_env() == frozenset()


def test_excluded_folders_from_env_blank_entries_filtered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APHRODITE_EXCLUDED_SHAREPOINT_FOLDERS", '["hr", "  ", ""]')
    assert handlers._excluded_folders_from_env() == frozenset({"hr"})


def _event_with_groups(groups_claim: Any) -> dict[str, Any]:
    return {"requestContext": {"authorizer": {"jwt": {"claims": {"cognito:groups": groups_claim}}}}}


def test_is_admin_true_for_json_array_claim() -> None:
    assert handlers._is_admin(_event_with_groups('["admins"]')) is True


def test_is_admin_true_for_plain_string_claim() -> None:
    assert handlers._is_admin(_event_with_groups("admins")) is True


def test_is_admin_false_for_other_group() -> None:
    assert handlers._is_admin(_event_with_groups('["staff"]')) is False


def test_is_admin_true_for_api_gateway_bracketed_space_separated_single_group() -> None:
    # API Gateway's HTTP API JWT authorizer serializes a multi-value claim
    # Go-style: "[admins]" for one group, "[admins staff]" for several —
    # never JSON, and never comma-separated.
    assert handlers._is_admin(_event_with_groups("[admins]")) is True


def test_is_admin_true_for_api_gateway_bracketed_space_separated_multiple_groups() -> None:
    assert handlers._is_admin(_event_with_groups("[staff admins]")) is True


def test_is_admin_false_for_api_gateway_bracketed_other_group() -> None:
    assert handlers._is_admin(_event_with_groups("[staff]")) is False


def test_is_admin_false_when_claim_missing() -> None:
    assert handlers._is_admin({"requestContext": {"authorizer": {"jwt": {"claims": {}}}}}) is False


def test_is_admin_false_when_claims_missing_entirely() -> None:
    assert handlers._is_admin({}) is False


def test_require_admin_returns_403_body_for_non_admin() -> None:
    resp = handlers._require_admin({})
    assert resp is not None and resp["statusCode"] == 403


def test_require_admin_returns_none_for_admin() -> None:
    assert handlers._require_admin(_event_with_groups('["admins"]')) is None


def admin_api_event(method: str, path: str, *, body: dict | None = None) -> dict:
    event = api_event(method, path, body=body)
    event["requestContext"]["authorizer"]["jwt"]["claims"]["cognito:groups"] = '["admins"]'
    return event


def test_connect_estateably_route_requires_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    install_deps(monkeypatch)
    resp = handlers.handler(api_event("POST", "/connectors/estateably", body={}))
    assert resp["statusCode"] == 403


def test_connect_estateably_route_success(monkeypatch: pytest.MonkeyPatch) -> None:
    install_deps(monkeypatch)
    monkeypatch.setattr(
        handlers.connectors_admin, "connect_estateably",
        lambda secrets, body, **kwargs: {"ok": True, "detail": "ok"},
    )
    resp = handlers.handler(
        admin_api_event("POST", "/connectors/estateably", body={"username": "u", "password": "p"})
    )
    assert resp["statusCode"] == 200


def test_capabilities_route_reflects_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    deps, _ = install_deps(monkeypatch)
    deps.connectors_enabled = frozenset({"clio"})
    deps.reports = None
    deps.aml_exports = object()
    deps.screener = None
    resp = handlers.handler(api_event("GET", "/capabilities"))
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["connectors"] == {
        "clio": True,
        "estateably": False,
        "quickbooks": False,
        "graph": False,
    }
    assert body["modules"] == {
        "quarterly_reports": False,
        "aml_export": True,
        "sanction_screener": False,
    }


def test_connect_estateably_route_rejects_disabled_connector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deps, _ = install_deps(monkeypatch)
    deps.connectors_enabled = frozenset({"clio"})  # estateably disabled
    resp = handlers.handler(
        admin_api_event("POST", "/connectors/estateably", body={"username": "u", "password": "p"})
    )
    assert resp["statusCode"] == 503


@pytest.mark.parametrize(
    "path,body",
    [
        ("/connectors/graph", {"tenant_id": "t", "client_id": "c", "client_secret": "s"}),
        ("/connectors/clio/authorize-url", {"client_id": "abc"}),
        ("/connectors/clio/exchange", {"client_id": "c", "client_secret": "s", "code": "code123"}),
        (
            "/connectors/quickbooks/start",
            {"client_id": "c", "client_secret": "s", "environment": "sandbox"},
        ),
    ],
)
def test_connector_route_rejects_disabled_connector(
    monkeypatch: pytest.MonkeyPatch, path: str, body: dict
) -> None:
    # Important (I4 from the Phase 2 final review): the estateably route had a
    # 503-when-disabled test, but the other four admin routes didn't.
    deps, _ = install_deps(monkeypatch)
    deps.connectors_enabled = frozenset()  # nothing enabled
    resp = handlers.handler(admin_api_event("POST", path, body=body))
    assert resp["statusCode"] == 503


def test_quickbooks_callback_route_rejects_disabled_connector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Important (I4): the unauthenticated callback is the security-relevant
    # one — a disabled connector must 503, not silently complete an OAuth
    # exchange into a secret nothing will ever read.
    deps, _ = install_deps(monkeypatch)
    deps.connectors_enabled = frozenset()
    event = api_event("GET", "/connectors/quickbooks/callback")
    event["queryStringParameters"] = {"code": "c", "realmId": "r", "state": "s"}
    resp = handlers.handler(event)
    assert resp["statusCode"] == 503


def test_connect_estateably_route_records_activity_against_the_real_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Important (I7): a credential write must leave a who-did-it audit trail,
    # not just a CloudTrail PutSecretValue attributed to the Lambda role.
    # Uses the real ActivityLog.record (not the looser FakeActivity double)
    # so a bad kwarg (e.g. one record() doesn't accept) fails this test.
    from backend.api.activity import ActivityLog

    class _FakeDdb:
        def put_item(self, *, TableName, Item):
            pass

    deps, _ = install_deps(monkeypatch)
    deps.activity = ActivityLog("t", client=_FakeDdb())
    monkeypatch.setattr(
        handlers.connectors_admin, "connect_estateably",
        lambda secrets, body, **kwargs: {"ok": True, "detail": "ok"},
    )
    resp = handlers.handler(
        admin_api_event("POST", "/connectors/estateably", body={"username": "u", "password": "p"})
    )
    assert resp["statusCode"] == 200


def test_connect_graph_route_requires_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    install_deps(monkeypatch)
    resp = handlers.handler(api_event("POST", "/connectors/graph", body={}))
    assert resp["statusCode"] == 403


def test_connect_graph_route_success(monkeypatch: pytest.MonkeyPatch) -> None:
    install_deps(monkeypatch)
    monkeypatch.setattr(
        handlers.connectors_admin, "connect_graph",
        lambda secrets, body, **kwargs: {"ok": True, "detail": "ok"},
    )
    resp = handlers.handler(
        admin_api_event(
            "POST",
            "/connectors/graph",
            body={"tenant_id": "t", "client_id": "c", "client_secret": "s"},
        )
    )
    assert resp["statusCode"] == 200


def test_clio_authorize_url_route_requires_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    install_deps(monkeypatch)
    resp = handlers.handler(api_event("POST", "/connectors/clio/authorize-url", body={}))
    assert resp["statusCode"] == 403


def test_clio_authorize_url_route_success(monkeypatch: pytest.MonkeyPatch) -> None:
    install_deps(monkeypatch)
    monkeypatch.setattr(
        handlers.connectors_admin, "clio_authorize_url",
        lambda body: {"url": "https://app.clio.com/oauth/authorize?x", "state": "s"},
    )
    resp = handlers.handler(
        admin_api_event("POST", "/connectors/clio/authorize-url", body={"client_id": "abc"})
    )
    assert resp["statusCode"] == 200


def test_clio_exchange_route_requires_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    install_deps(monkeypatch)
    resp = handlers.handler(api_event("POST", "/connectors/clio/exchange", body={}))
    assert resp["statusCode"] == 403


def test_clio_exchange_route_success(monkeypatch: pytest.MonkeyPatch) -> None:
    install_deps(monkeypatch)
    monkeypatch.setattr(
        handlers.connectors_admin, "clio_exchange",
        lambda secrets, body, **kwargs: {"ok": True, "detail": "ok"},
    )
    resp = handlers.handler(
        admin_api_event(
            "POST", "/connectors/clio/exchange",
            body={"client_id": "c", "client_secret": "s", "code": "code123"},
        )
    )
    assert resp["statusCode"] == 200


def test_quickbooks_start_route_requires_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    install_deps(monkeypatch)
    resp = handlers.handler(api_event("POST", "/connectors/quickbooks/start", body={}))
    assert resp["statusCode"] == 403


def test_quickbooks_start_route_success(monkeypatch: pytest.MonkeyPatch) -> None:
    install_deps(monkeypatch)
    monkeypatch.setenv("APHRODITE_QUICKBOOKS_REDIRECT_URI", "https://api.example/connectors/quickbooks/callback")
    monkeypatch.setattr(
        handlers.connectors_admin, "quickbooks_start",
        lambda secrets, body, *, redirect_uri: {"url": "https://appcenter.intuit.com/connect/oauth2?x"},
    )
    resp = handlers.handler(
        admin_api_event(
            "POST", "/connectors/quickbooks/start",
            body={"client_id": "c", "client_secret": "s", "environment": "sandbox"},
        )
    )
    assert resp["statusCode"] == 200


def test_build_deps_wires_sns_alert_when_topic_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APHRODITE_JOBS_TABLE", "jobs")
    monkeypatch.setenv("APHRODITE_SELF_FUNCTION", "fn")
    monkeypatch.setenv("APHRODITE_INTEGRATION_HEALTH_TABLE", "health")
    monkeypatch.setenv(
        "APHRODITE_INTEGRATIONS_ALERT_TOPIC_ARN", "arn:aws:sns:ca-central-1:123:topic"
    )
    deps = handlers._build_deps()
    assert deps.integrations is not None
    assert deps.integrations.on_alert is not None


def test_build_deps_leaves_alert_unwired_when_topic_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APHRODITE_JOBS_TABLE", "jobs")
    monkeypatch.setenv("APHRODITE_SELF_FUNCTION", "fn")
    monkeypatch.setenv("APHRODITE_INTEGRATION_HEALTH_TABLE", "health")
    monkeypatch.delenv("APHRODITE_INTEGRATIONS_ALERT_TOPIC_ARN", raising=False)
    deps = handlers._build_deps()
    assert deps.integrations is not None
    assert deps.integrations.on_alert is None


def test_quickbooks_callback_route_redirects_without_admin_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_deps(monkeypatch)
    monkeypatch.setenv(
        "APHRODITE_QUICKBOOKS_REDIRECT_URI", "https://api.example/connectors/quickbooks/callback"
    )
    monkeypatch.setenv("APHRODITE_APP_BASE_URL", "https://app.example")
    monkeypatch.setattr(
        handlers.connectors_admin, "quickbooks_callback",
        lambda *a, **k: "https://app.example/?setup=quickbooks-connected",
    )
    event = api_event("GET", "/connectors/quickbooks/callback")
    event["queryStringParameters"] = {"code": "c", "realmId": "r", "state": "s"}
    resp = handlers.handler(event)
    assert resp["statusCode"] == 302
    assert resp["headers"]["Location"] == "https://app.example/?setup=quickbooks-connected"


# -- Deployment config (GET/PUT /config) -------------------------------------


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


# -- Per-request config resolution in the search worker ---------------------


def _record_worker_overrides(
    monkeypatch: pytest.MonkeyPatch, deps: handlers.Deps
) -> list[dict[str, Any]]:
    """Record the config overrides each worker run passes to build + synthesize."""
    seen: list[dict[str, Any]] = []
    real_build = deps.dossier_builder.build
    real_synthesize = deps.synthesizer.synthesize

    def recording_build(c: Any, **kwargs: Any) -> Any:
        seen.append({"excluded_sharepoint_folders": kwargs.get("excluded_sharepoint_folders")})
        return real_build(c, **kwargs)

    def recording_synthesize(d: Any, q: str, **kwargs: Any) -> Any:
        seen[-1]["org_name"] = kwargs.get("org_name")
        seen[-1]["office_address"] = kwargs.get("office_address")
        return real_synthesize(d, q, **kwargs)

    monkeypatch.setattr(deps.dossier_builder, "build", recording_build)
    monkeypatch.setattr(deps.synthesizer, "synthesize", recording_synthesize)
    return seen


def _run_worker_job(deps: handlers.Deps, job_id: str) -> None:
    deps.store.create(job_id, "Smith", {"sub": "u"})
    out = handlers.handler({"worker": True, "job_id": job_id, "query": "Smith", "actor": {}})
    assert out["ok"] is True


def test_config_change_is_visible_to_the_next_request_without_a_new_deps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Review Focus. One Deps (a warm container) serves two searches. Between them an
    # admin saves new values — through a *different* ConfigClient on the same SSM
    # store, because in production the PUT /config request and the async search
    # worker run in different Lambda containers. Saving through deps.config itself
    # would update its in-memory cache and pass even if the worker never re-read SSM.
    from services.config import ConfigClient

    monkeypatch.delenv("APHRODITE_ORG_NAME", raising=False)
    monkeypatch.delenv("APHRODITE_ORG_OFFICE_ADDRESS", raising=False)
    monkeypatch.delenv("APHRODITE_EXCLUDED_SHAREPOINT_FOLDERS", raising=False)
    ssm = _FakeSsmForConfig()
    deps, _ = install_deps(monkeypatch)
    deps.config = ConfigClient(client=ssm)
    seen = _record_worker_overrides(monkeypatch, deps)

    _run_worker_job(deps, "j1")
    ConfigClient(client=ssm).put(
        {
            "org_name": "Acme Trust",
            "org_office_address": "1 Main St",
            "excluded_sharepoint_folders": ["Legal", "Payroll"],
        }
    )
    _run_worker_job(deps, "j2")

    assert seen[0] == {
        "excluded_sharepoint_folders": scoping.DEFAULT_EXCLUDED_FOLDERS,
        "org_name": handlers.DEFAULT_ORG_NAME,
        "office_address": handlers.DEFAULT_ORG_OFFICE_ADDRESS,
    }
    assert seen[1] == {
        "excluded_sharepoint_folders": frozenset({"legal", "payroll"}),
        "org_name": "Acme Trust",
        "office_address": "1 Main St",
    }


def test_worker_honors_a_stored_empty_exclusion_list(monkeypatch: pytest.MonkeyPatch) -> None:
    # Presence (``in``), not truthiness: an admin who clears the list to [] means
    # "exclude nothing" — it must not silently fall back to the env var.
    from services.config import ConfigClient

    monkeypatch.setenv("APHRODITE_EXCLUDED_SHAREPOINT_FOLDERS", '["hr"]')
    ssm = _FakeSsmForConfig()
    deps, _ = install_deps(monkeypatch)
    deps.config = ConfigClient(client=ssm)
    ConfigClient(client=ssm).put({"excluded_sharepoint_folders": []})
    seen = _record_worker_overrides(monkeypatch, deps)

    _run_worker_job(deps, "j1")

    assert seen[0]["excluded_sharepoint_folders"] == frozenset()


def test_worker_falls_back_to_env_when_a_key_is_not_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.config import ConfigClient

    monkeypatch.setenv("APHRODITE_ORG_NAME", "Env Trust")
    monkeypatch.setenv("APHRODITE_ORG_OFFICE_ADDRESS", "2 Env Rd")
    monkeypatch.setenv("APHRODITE_EXCLUDED_SHAREPOINT_FOLDERS", "hr, payroll")
    ssm = _FakeSsmForConfig()
    deps, _ = install_deps(monkeypatch)
    deps.config = ConfigClient(client=ssm)
    ConfigClient(client=ssm).put({"org_name": "Stored Trust"})
    seen = _record_worker_overrides(monkeypatch, deps)

    _run_worker_job(deps, "j1")

    assert seen[0] == {
        "excluded_sharepoint_folders": frozenset({"hr", "payroll"}),
        "org_name": "Stored Trust",
        "office_address": "2 Env Rd",
    }


def test_worker_without_a_config_client_uses_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APHRODITE_ORG_NAME", "Env Trust")
    monkeypatch.delenv("APHRODITE_ORG_OFFICE_ADDRESS", raising=False)
    monkeypatch.delenv("APHRODITE_EXCLUDED_SHAREPOINT_FOLDERS", raising=False)
    deps, _ = install_deps(monkeypatch)
    assert deps.config is None
    seen = _record_worker_overrides(monkeypatch, deps)

    _run_worker_job(deps, "j1")

    assert seen[0] == {
        "excluded_sharepoint_folders": scoping.DEFAULT_EXCLUDED_FOLDERS,
        "org_name": "Env Trust",
        "office_address": handlers.DEFAULT_ORG_OFFICE_ADDRESS,
    }
