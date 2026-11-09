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


class _FakeIntegrations:
    """Records record_result calls so a connect/reconnect route can be proven
    to update the health store immediately (Review Focus #4 / design doc's
    "no looks-connected-but-broken" goal), instead of waiting up to an hour."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def record_result(self, integration, *, ok, detail="", suggested_fix=""):
        self.calls.append(
            {"integration": integration, "ok": ok, "detail": detail, "suggested_fix": suggested_fix}
        )


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
def test_connect_estateably_records_health_result(monkeypatch):
    # Important: a successful reconnect must update the status display
    # immediately, not wait up to an hour for the next scheduled probe.
    responses.add(
        responses.POST,
        "https://app.estateably.com/api/v1/auth/login",
        json={"data": {"attributes": {"token": "t"}}},
        status=200,
    )
    monkeypatch.setattr(connectors_admin, "run_check", lambda name, secrets: {"ok": True, "detail": "ok"})
    secrets = _FakeSecrets()
    integrations = _FakeIntegrations()
    connectors_admin.connect_estateably(
        secrets, {"username": "u", "password": "p"}, integrations=integrations
    )
    assert integrations.calls == [
        {"integration": "estateably", "ok": True, "detail": "ok", "suggested_fix": ""}
    ]


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
def test_connect_graph_records_health_result(monkeypatch):
    responses.add(responses.POST, _GRAPH_TOKEN_URL, json={"access_token": "at", "expires_in": 3600}, status=200)
    monkeypatch.setattr(connectors_admin, "run_check", lambda name, secrets: {"ok": True, "detail": "ok"})
    secrets = _FakeSecrets()
    integrations = _FakeIntegrations()
    connectors_admin.connect_graph(
        secrets, {"tenant_id": "t", "client_id": "c", "client_secret": "s"}, integrations=integrations
    )
    assert integrations.calls == [
        {"integration": "graph", "ok": True, "detail": "ok", "suggested_fix": ""}
    ]


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


# -- Clio -------------------------------------------------------------------


def test_clio_authorize_url_builds_oob_link():
    result = connectors_admin.clio_authorize_url({"client_id": "abc"})
    assert result["url"].startswith("https://app.clio.com/oauth/authorize?")
    assert "state" in result


def test_clio_authorize_url_missing_client_id_rejected():
    with pytest.raises(connectors_admin.ConnectorValidationError):
        connectors_admin.clio_authorize_url({})


@responses.activate
def test_clio_exchange_stores_and_validates_on_success(monkeypatch):
    responses.add(
        responses.POST,
        "https://app.clio.com/oauth/token",
        json={"refresh_token": "rt", "access_token": "at"},
        status=200,
    )
    monkeypatch.setattr(connectors_admin, "run_check", lambda name, secrets: {"ok": True, "detail": "ok"})
    secrets = _FakeSecrets()
    result = connectors_admin.clio_exchange(secrets, {"client_id": "c", "client_secret": "s", "code": "code123"})
    assert secrets.stored["aphrodite/clio"]["refresh_token"] == "rt"
    assert result == {"ok": True, "detail": "ok"}


@responses.activate
def test_clio_exchange_records_health_result(monkeypatch):
    responses.add(
        responses.POST,
        "https://app.clio.com/oauth/token",
        json={"refresh_token": "rt", "access_token": "at"},
        status=200,
    )
    monkeypatch.setattr(connectors_admin, "run_check", lambda name, secrets: {"ok": True, "detail": "ok"})
    secrets = _FakeSecrets()
    integrations = _FakeIntegrations()
    connectors_admin.clio_exchange(
        secrets, {"client_id": "c", "client_secret": "s", "code": "code123"}, integrations=integrations
    )
    assert integrations.calls == [
        {"integration": "clio", "ok": True, "detail": "ok", "suggested_fix": ""}
    ]


@responses.activate
def test_clio_exchange_failure_stores_nothing():
    responses.add(responses.POST, "https://app.clio.com/oauth/token", json={}, status=400)
    secrets = _FakeSecrets()
    with pytest.raises(connectors_admin.ConnectorValidationError):
        connectors_admin.clio_exchange(secrets, {"client_id": "c", "client_secret": "s", "code": "bad"})
    assert secrets.stored == {}


# -- QuickBooks ---------------------------------------------------------


def test_quickbooks_start_stores_pending_secret_and_returns_url():
    secrets = _FakeSecrets()
    result = connectors_admin.quickbooks_start(
        secrets, {"client_id": "c", "client_secret": "s", "environment": "sandbox"},
        redirect_uri="https://api.example/connectors/quickbooks/callback",
    )
    assert result["url"].startswith("https://appcenter.intuit.com/connect/oauth2?")
    stored = secrets.stored["aphrodite/quickbooks"]
    assert stored["_pending"]["client_id"] == "c" and "state" in stored["_pending"]


def test_quickbooks_start_preserves_existing_credential_in_pending():
    # Review Focus #2 / Global Constraint: starting a reconnect must not
    # touch the currently-working credential until the callback succeeds.
    secrets = _FakeSecrets()
    secrets.stored["aphrodite/quickbooks"] = {
        "client_id": "old-c", "client_secret": "old-s", "environment": "sandbox",
        "refresh_token": "old-rt", "realm_id": "old-realm",
    }
    connectors_admin.quickbooks_start(
        secrets, {"client_id": "new-c", "client_secret": "new-s", "environment": "sandbox"},
        redirect_uri="https://api.example/connectors/quickbooks/callback",
    )
    stored = secrets.stored["aphrodite/quickbooks"]
    assert stored["refresh_token"] == "old-rt"
    assert stored["realm_id"] == "old-realm"
    assert stored["_pending"]["client_id"] == "new-c"


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
def test_quickbooks_callback_matching_state_completes_exchange(monkeypatch):
    secrets = _FakeSecrets()
    secrets.stored["aphrodite/quickbooks"] = {
        "_pending": {"client_id": "c", "client_secret": "s", "environment": "sandbox", "state": "abc"},
    }
    responses.add(
        responses.POST,
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        json={"refresh_token": "rt", "access_token": "at"},
        status=200,
    )
    monkeypatch.setattr(connectors_admin, "run_check", lambda name, secrets: {"ok": True, "detail": "ok"})
    url = connectors_admin.quickbooks_callback(
        secrets, {"code": "code1", "realmId": "r1", "state": "abc"},
        redirect_uri="https://api.example/connectors/quickbooks/callback",
        app_base_url="https://app.example",
    )
    assert url == "https://app.example/?setup=quickbooks-connected"
    stored = secrets.stored["aphrodite/quickbooks"]
    assert stored["refresh_token"] == "rt" and stored["realm_id"] == "r1" and "_pending" not in stored
    assert stored["client_id"] == "c" and stored["client_secret"] == "s"


@responses.activate
def test_quickbooks_callback_records_health_result(monkeypatch):
    secrets = _FakeSecrets()
    secrets.stored["aphrodite/quickbooks"] = {
        "_pending": {"client_id": "c", "client_secret": "s", "environment": "sandbox", "state": "abc"},
    }
    responses.add(
        responses.POST,
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        json={"refresh_token": "rt", "access_token": "at"},
        status=200,
    )
    monkeypatch.setattr(connectors_admin, "run_check", lambda name, secrets: {"ok": True, "detail": "ok"})
    integrations = _FakeIntegrations()
    connectors_admin.quickbooks_callback(
        secrets, {"code": "code1", "realmId": "r1", "state": "abc"},
        redirect_uri="https://api.example/connectors/quickbooks/callback",
        app_base_url="https://app.example",
        integrations=integrations,
    )
    assert integrations.calls == [
        {"integration": "quickbooks", "ok": True, "detail": "ok", "suggested_fix": ""}
    ]


def test_quickbooks_callback_mismatched_state_does_not_complete():
    secrets = _FakeSecrets()
    secrets.stored["aphrodite/quickbooks"] = {
        "client_id": "old-c", "refresh_token": "old-rt", "realm_id": "old-realm",
        "_pending": {"client_id": "c", "client_secret": "s", "environment": "sandbox", "state": "abc"},
    }
    url = connectors_admin.quickbooks_callback(
        secrets, {"code": "code1", "realmId": "r1", "state": "WRONG"},
        redirect_uri="https://api.example/connectors/quickbooks/callback",
        app_base_url="https://app.example",
    )
    assert url == "https://app.example/?setup=quickbooks-error"
    stored = secrets.stored["aphrodite/quickbooks"]
    assert stored["refresh_token"] == "old-rt" and stored["realm_id"] == "old-realm"
    assert "_pending" not in stored  # cleared so a replay of the stale link can't complete


def test_quickbooks_callback_failure_leaves_existing_credential_untouched():
    # Critical: a cancelled/failed reconnect must not destroy a working connection.
    secrets = _FakeSecrets()
    secrets.stored["aphrodite/quickbooks"] = {
        "client_id": "old-c", "client_secret": "old-s", "environment": "sandbox",
        "refresh_token": "old-rt", "realm_id": "old-realm",
        "_pending": {"client_id": "new-c", "client_secret": "new-s", "environment": "sandbox", "state": "abc"},
    }
    url = connectors_admin.quickbooks_callback(
        secrets, {"code": "", "realmId": "r1", "state": "abc"},  # missing code
        redirect_uri="https://api.example/connectors/quickbooks/callback",
        app_base_url="https://app.example",
    )
    assert url == "https://app.example/?setup=quickbooks-error"
    stored = secrets.stored["aphrodite/quickbooks"]
    assert stored["client_id"] == "old-c" and stored["refresh_token"] == "old-rt"


@responses.activate
def test_quickbooks_callback_missing_realm_id_rejected(monkeypatch):
    secrets = _FakeSecrets()
    secrets.stored["aphrodite/quickbooks"] = {
        "refresh_token": "old-rt", "realm_id": "old-realm",
        "_pending": {"client_id": "c", "client_secret": "s", "environment": "sandbox", "state": "abc"},
    }
    responses.add(
        responses.POST,
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        json={"refresh_token": "rt", "access_token": "at"},
        status=200,
    )
    monkeypatch.setattr(connectors_admin, "run_check", lambda name, secrets: {"ok": True, "detail": "ok"})
    url = connectors_admin.quickbooks_callback(
        secrets, {"code": "code1", "state": "abc"},  # no realmId
        redirect_uri="https://api.example/connectors/quickbooks/callback",
        app_base_url="https://app.example",
    )
    assert url == "https://app.example/?setup=quickbooks-error"
    stored = secrets.stored["aphrodite/quickbooks"]
    assert stored["refresh_token"] == "old-rt" and stored["realm_id"] == "old-realm"
