"""Unit tests for the matters service + /matters and /reports/generate routes."""

from __future__ import annotations

import json

from backend.api import handlers
from backend.api.matters import MattersService, QuarterlyTrigger


class FakeClio:
    def __init__(self, matters):
        self._matters = matters
        self.calls: list[dict] = []

    def list_matters(self, *, status="open", fields=None, **params):
        self.calls.append({"status": status, "fields": fields})
        return self._matters


class FakeLambda:
    def __init__(self):
        self.invocations: list[dict] = []

    def invoke(self, **kwargs):
        self.invocations.append(kwargs)
        return {"StatusCode": 202}


def test_list_open_shapes_matters():
    clio = FakeClio(
        [
            {
                "id": 100001,
                "display_number": "260012",
                "description": "Burgess Trust",
                "client": {"id": 7, "name": "John Dalton Burgess"},
            },
            {"id": 100002, "display_number": "250043", "description": "Estate of Doerges"},
        ]
    )
    rows = MattersService(clio).list_open()
    assert rows[0] == {
        "matter_id": "100001",
        "matter_number": "260012",
        "description": "Burgess Trust",
        "client_name": "John Dalton Burgess",
    }
    assert rows[1]["client_name"] == ""  # missing client tolerated
    assert clio.calls[0]["status"] == "open"


def test_trigger_async_invokes_with_period():
    lam = FakeLambda()
    QuarterlyTrigger(lam, "aphrodite-quarterly-report").generate("260012", 2026, 2)
    inv = lam.invocations[0]
    assert inv["FunctionName"] == "aphrodite-quarterly-report"
    assert inv["InvocationType"] == "Event"
    payload = json.loads(inv["Payload"])
    assert payload == {"matter_ids": ["260012"], "period": {"year": 2026, "quarter": 2}}


# -- routes ------------------------------------------------------------------


def _install(monkeypatch, *, matters=None, quarterly=None):
    deps = handlers.Deps(
        resolver=object(),
        dossier_builder=object(),
        synthesizer=object(),
        store=object(),
        self_invoke=lambda p: None,
        matters=matters,
        quarterly=quarterly,
    )
    monkeypatch.setattr(handlers, "_DEPS", deps)


def _event(method, path, *, body=None):
    return {
        "requestContext": {
            "http": {"method": method, "path": path},
            "authorizer": {"jwt": {"claims": {"sub": "u1", "email": "a@b.ca"}}},
        },
        "body": json.dumps(body) if body is not None else None,
    }


def test_matters_route(monkeypatch):
    clio = FakeClio([{"id": 1, "display_number": "A", "description": "d"}])
    _install(monkeypatch, matters=MattersService(clio))
    resp = handlers.handler(_event("GET", "/matters"))
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["matters"][0]["matter_number"] == "A"


def test_generate_route_triggers(monkeypatch):
    lam = FakeLambda()
    _install(monkeypatch, quarterly=QuarterlyTrigger(lam, "fn"))
    resp = handlers.handler(
        _event(
            "POST", "/reports/generate", body={"matter_id": "260012", "year": 2026, "quarter": 2}
        )
    )
    assert resp["statusCode"] == 202
    assert json.loads(resp["body"])["matter_id"] == "260012"
    assert len(lam.invocations) == 1


def test_generate_route_validates(monkeypatch):
    _install(monkeypatch, quarterly=QuarterlyTrigger(FakeLambda(), "fn"))
    # missing quarter
    assert (
        handlers.handler(_event("POST", "/reports/generate", body={"matter_id": "1"}))["statusCode"]
        == 400
    )
    # bad quarter
    assert (
        handlers.handler(
            _event("POST", "/reports/generate", body={"matter_id": "1", "year": 2026, "quarter": 9})
        )["statusCode"]
        == 400
    )


def test_routes_503_when_unconfigured(monkeypatch):
    _install(monkeypatch, matters=None, quarterly=None)
    assert handlers.handler(_event("GET", "/matters"))["statusCode"] == 503
    assert (
        handlers.handler(
            _event("POST", "/reports/generate", body={"matter_id": "1", "year": 2026, "quarter": 2})
        )["statusCode"]
        == 503
    )
