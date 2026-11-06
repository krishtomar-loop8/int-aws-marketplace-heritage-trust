"""Contract tests for connector onboarding's validate-then-store logic.

No network except what ``responses`` stubs, no AWS — a tiny in-memory fake
stands in for Secrets Manager.
"""

from __future__ import annotations

import pytest
import responses

from backend.api import connectors_admin


class _FakeSecrets:
    def __init__(self) -> None:
        self.stored: dict[str, dict] = {}

    def get(self, name, *, refresh: bool = False):
        return self.stored[name]

    def put(self, name, secret) -> None:
        self.stored[name] = secret


# -- Estateably ---------------------------------------------------------


@responses.activate
def test_connect_estateably_stores_and_validates_on_success(monkeypatch):
    responses.add(
        responses.POST,
        "https://app.estateably.com/api/v1/auth/login",
        json={"data": {"attributes": {"token": "t"}}},
        status=200,
    )
    monkeypatch.setattr(connectors_admin, "run_check", lambda name, secrets: {"ok": True, "detail": "ok"})
    secrets = _FakeSecrets()
    result = connectors_admin.connect_estateably(secrets, {"username": "u", "password": "p"})
    assert result == {"ok": True, "detail": "ok"}
    assert secrets.stored["aphrodite/estateably"] == {"username": "u", "password": "p"}


@responses.activate
def test_connect_estateably_bad_credentials_stores_nothing():
    responses.add(
        responses.POST,
        "https://app.estateably.com/api/v1/auth/login",
        json={},
        status=401,
    )
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
    responses.add(
        responses.POST,
        "https://app.estateably.com/api/v1/auth/login",
        json={"data": {"attributes": {"token": "t1"}}},
        status=200,
    )
    responses.add(
        responses.POST,
        "https://app.estateably.com/api/v1/auth/login",
        json={"data": {"attributes": {"token": "t2"}}},
        status=200,
    )
    monkeypatch.setattr(connectors_admin, "run_check", lambda name, secrets: {"ok": True, "detail": "ok"})
    secrets = _FakeSecrets()
    body = {"username": "u", "password": "p"}
    first = connectors_admin.connect_estateably(secrets, body)
    second = connectors_admin.connect_estateably(secrets, body)
    assert first == second == {"ok": True, "detail": "ok"}


# -- Graph ----------------------------------------------------------------

_GRAPH_TOKEN_URL = "https://login.microsoftonline.com/t/oauth2/v2.0/token"


@responses.activate
def test_connect_graph_stores_and_validates_on_success(monkeypatch):
    responses.add(responses.POST, _GRAPH_TOKEN_URL, json={"access_token": "at", "expires_in": 3600}, status=200)
    monkeypatch.setattr(connectors_admin, "run_check", lambda name, secrets: {"ok": True, "detail": "ok"})
    secrets = _FakeSecrets()
    body = {"tenant_id": "t", "client_id": "c", "client_secret": "s"}
    result = connectors_admin.connect_graph(secrets, body)
    assert result == {"ok": True, "detail": "ok"}
    assert secrets.stored["aphrodite/graph"] == body


@responses.activate
def test_connect_graph_bad_credentials_stores_nothing():
    responses.add(responses.POST, _GRAPH_TOKEN_URL, json={"error": "invalid_client"}, status=401)
    secrets = _FakeSecrets()
    with pytest.raises(connectors_admin.ConnectorValidationError):
        connectors_admin.connect_graph(secrets, {"tenant_id": "t", "client_id": "c", "client_secret": "wrong"})
    assert secrets.stored == {}


def test_connect_graph_missing_fields_rejected():
    secrets = _FakeSecrets()
    with pytest.raises(connectors_admin.ConnectorValidationError):
        connectors_admin.connect_graph(secrets, {"tenant_id": "", "client_id": "", "client_secret": ""})
    assert secrets.stored == {}
