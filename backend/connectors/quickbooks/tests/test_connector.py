"""Contract tests for the read-only QuickBooks Online connector.

No network, AWS, or real credentials are touched: HTTP is mocked with
``responses`` and secrets come from an in-memory stub.
"""

from __future__ import annotations

from typing import Any

import pytest
import responses

from backend.connectors.quickbooks import QuickBooksConnector
from services.http import HttpError

TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
SANDBOX_BASE = "https://sandbox-quickbooks.api.intuit.com"
PRODUCTION_BASE = "https://quickbooks.api.intuit.com"
REALM = "1234567890"


class FakeSecretsClient:
    """In-memory stand-in for services.secrets.SecretsClient.

    Mirrors the real client's warm-container cache vs Secrets Manager split:
    ``store`` is "Secrets Manager" and can be mutated by a test to simulate a
    CONCURRENT Lambda rotating the token behind this container's back; ``get``
    serves the cached copy unless ``refresh=True`` re-reads the store.
    """

    def __init__(self, secret: dict[str, Any]) -> None:
        self.store = dict(secret)
        self._cache = dict(secret)
        self.puts: list[tuple[str, dict[str, Any]]] = []

    def get(self, name: str, *, refresh: bool = False) -> dict[str, Any]:
        if refresh:
            self._cache = dict(self.store)
        return self._cache

    def put(self, name: str, secret: dict[str, Any]) -> None:
        self.puts.append((name, dict(secret)))
        self.store = dict(secret)
        self._cache = dict(secret)


def _secret(environment: str = "sandbox") -> dict[str, Any]:
    return {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "refresh_token": "refresh-token-0",
        "realm_id": REALM,
        "environment": environment,
    }


def _add_token(refresh_token: str = "refresh-token-1") -> None:
    responses.add(
        responses.POST,
        TOKEN_URL,
        json={
            "access_token": "access-token-abc",
            "refresh_token": refresh_token,
            "expires_in": 3600,
            "token_type": "bearer",
        },
        status=200,
    )


def _connector(environment: str = "sandbox") -> QuickBooksConnector:
    return QuickBooksConnector(secrets=FakeSecretsClient(_secret(environment)))


@responses.activate
def test_oauth_refresh_uses_basic_auth_and_form_grant():
    _add_token()
    responses.add(
        responses.GET,
        f"{SANDBOX_BASE}/v3/company/{REALM}/companyinfo/{REALM}",
        json={"CompanyInfo": {"CompanyName": "Aphrodite"}},
        status=200,
    )

    connector = _connector()
    connector.get_company_info()

    token_call = responses.calls[0].request
    assert token_call.url == TOKEN_URL
    assert token_call.method == "POST"
    # HTTP Basic of client_id:client_secret
    assert token_call.headers["Authorization"] == "Basic Y2xpZW50LWlkOmNsaWVudC1zZWNyZXQ="
    assert token_call.headers["Accept"] == "application/json"
    assert "grant_type=refresh_token" in token_call.body
    assert "refresh_token=refresh-token-0" in token_call.body


@responses.activate
def test_lazy_auth_then_token_reuse_across_calls():
    _add_token()
    responses.add(
        responses.GET,
        f"{SANDBOX_BASE}/v3/company/{REALM}/companyinfo/{REALM}",
        json={"CompanyInfo": {"CompanyName": "Aphrodite"}},
        status=200,
    )
    responses.add(
        responses.GET,
        f"{SANDBOX_BASE}/v3/company/{REALM}/query",
        json={"QueryResponse": {"Account": [{"Id": "1"}]}},
        status=200,
    )

    connector = _connector()
    connector.get_company_info()
    connector.list_accounts()

    # Exactly one token refresh; the bearer is reused for both GETs.
    token_calls = [c for c in responses.calls if c.request.url == TOKEN_URL]
    assert len(token_calls) == 1

    api_call = responses.calls[1].request
    assert api_call.headers["Authorization"] == "Bearer access-token-abc"


@responses.activate
def test_get_company_info_unwraps_entity():
    _add_token()
    responses.add(
        responses.GET,
        f"{SANDBOX_BASE}/v3/company/{REALM}/companyinfo/{REALM}",
        json={"CompanyInfo": {"CompanyName": "Aphrodite", "Id": REALM}},
        status=200,
    )

    info = _connector().get_company_info()
    assert info == {"CompanyName": "Aphrodite", "Id": REALM}


@responses.activate
def test_query_invoices_returns_entity_list():
    _add_token()
    responses.add(
        responses.GET,
        f"{SANDBOX_BASE}/v3/company/{REALM}/query",
        json={
            "QueryResponse": {
                "Invoice": [{"Id": "10", "TotalAmt": 500.0}, {"Id": "11", "TotalAmt": 250.0}],
                "maxResults": 2,
            }
        },
        status=200,
    )

    invoices = _connector().list_invoices()
    assert [i["Id"] for i in invoices] == ["10", "11"]

    sent = responses.calls[-1].request
    assert "SELECT+%2A+FROM+Invoice" in sent.url or "SELECT * FROM Invoice" in sent.url


@responses.activate
def test_find_customers_builds_like_query_and_escapes_quotes():
    _add_token()
    responses.add(
        responses.GET,
        f"{SANDBOX_BASE}/v3/company/{REALM}/query",
        json={"QueryResponse": {"Customer": [{"Id": "7", "DisplayName": "O'Rourke Estate"}]}},
        status=200,
    )

    customers = _connector().find_customers("O'Rourke")
    assert [c["Id"] for c in customers] == ["7"]

    sent = responses.calls[-1].request.url
    # single quote doubled for the QBO string literal
    assert "O%27%27Rourke" in sent or "O''Rourke" in sent
    assert "FROM+Customer" in sent or "FROM Customer" in sent


@responses.activate
def test_find_customer_by_clio_id_queries_the_clio_token():
    _add_token()
    responses.add(
        responses.GET,
        f"{SANDBOX_BASE}/v3/company/{REALM}/query",
        json={
            "QueryResponse": {"Customer": [{"Id": "9", "DisplayName": "Smith Estate (Clio 123)"}]}
        },
        status=200,
    )

    customers = _connector().find_customer_by_clio_id("123")
    assert [c["Id"] for c in customers] == ["9"]

    sent = responses.calls[-1].request.url
    assert "FROM+Customer" in sent or "FROM Customer" in sent
    assert "Clio" in sent and "123" in sent  # matches the "(Clio 123)" DisplayName token


@responses.activate
def test_list_invoices_scoped_to_customer():
    _add_token()
    responses.add(
        responses.GET,
        f"{SANDBOX_BASE}/v3/company/{REALM}/query",
        json={"QueryResponse": {"Invoice": [{"Id": "10", "TotalAmt": 500.0}]}},
        status=200,
    )

    _connector().list_invoices(customer_id="42")

    sent = responses.calls[-1].request.url
    assert "CustomerRef+%3D+%2742%27" in sent or "CustomerRef = '42'" in sent


@responses.activate
def test_query_empty_response_returns_empty_list():
    _add_token()
    responses.add(
        responses.GET,
        f"{SANDBOX_BASE}/v3/company/{REALM}/query",
        json={"QueryResponse": {}},
        status=200,
    )

    assert _connector().query("SELECT * FROM Payment") == []


@responses.activate
def test_production_environment_uses_production_base_url():
    _add_token()
    responses.add(
        responses.GET,
        f"{PRODUCTION_BASE}/v3/company/{REALM}/companyinfo/{REALM}",
        json={"CompanyInfo": {"CompanyName": "Aphrodite"}},
        status=200,
    )

    connector = _connector(environment="production")
    assert connector.base_url == PRODUCTION_BASE
    connector.get_company_info()
    assert responses.calls[1].request.url.startswith(PRODUCTION_BASE)


def test_unknown_environment_raises():
    connector = _connector(environment="staging")
    with pytest.raises(ValueError):
        _ = connector.base_url


@responses.activate
def test_token_refresh_failure_raises_http_error():
    responses.add(responses.POST, TOKEN_URL, json={"error": "invalid_grant"}, status=400)

    with pytest.raises(HttpError):
        _connector().get_company_info()


@responses.activate
def test_api_401_raises_http_error():
    _add_token()
    responses.add(
        responses.GET,
        f"{SANDBOX_BASE}/v3/company/{REALM}/companyinfo/{REALM}",
        json={"fault": "AuthenticationFailed"},
        status=401,
    )

    with pytest.raises(HttpError):
        _connector().get_company_info()


@responses.activate
def test_rotated_refresh_token_is_persisted_to_secrets():
    # Intuit rotates the refresh token; it must be written back so the next cold
    # start doesn't reload a stale (now-invalid) token.
    _add_token(refresh_token="refresh-token-ROTATED")
    responses.add(
        responses.GET,
        f"{SANDBOX_BASE}/v3/company/{REALM}/companyinfo/{REALM}",
        json={"CompanyInfo": {"CompanyName": "Aphrodite"}},
        status=200,
    )

    secrets = FakeSecretsClient(_secret())
    QuickBooksConnector(secrets=secrets).get_company_info()

    assert secrets.puts, "rotated refresh token should be persisted"
    name, stored = secrets.puts[-1]
    assert name == "aphrodite/quickbooks"
    assert stored["refresh_token"] == "refresh-token-ROTATED"


@responses.activate
def test_unchanged_refresh_token_is_not_persisted():
    # No write when Intuit returns the same refresh token (avoid churn).
    _add_token(refresh_token="refresh-token-0")
    responses.add(
        responses.GET,
        f"{SANDBOX_BASE}/v3/company/{REALM}/companyinfo/{REALM}",
        json={"CompanyInfo": {"CompanyName": "Aphrodite"}},
        status=200,
    )

    secrets = FakeSecretsClient(_secret())
    QuickBooksConnector(secrets=secrets).get_company_info()

    assert secrets.puts == []


@responses.activate
def test_refresh_400_retries_once_with_concurrently_rotated_stored_token():
    # The round-2 outage: another Lambda rotated the refresh token (Intuit
    # invalidates the prior one), so this container's CACHED token 400s. The
    # connector must re-read the secret and retry once with the newer token.
    responses.add(responses.POST, TOKEN_URL, json={"error": "invalid_grant"}, status=400)
    _add_token(refresh_token="refresh-token-2")
    responses.add(
        responses.GET,
        f"{SANDBOX_BASE}/v3/company/{REALM}/companyinfo/{REALM}",
        json={"CompanyInfo": {"CompanyName": "Aphrodite"}},
        status=200,
    )

    secrets = FakeSecretsClient(_secret())  # cache holds refresh-token-0
    secrets.store["refresh_token"] = "refresh-token-1"  # concurrent rotation landed

    QuickBooksConnector(secrets=secrets).get_company_info()

    token_calls = [c for c in responses.calls if c.request.url == TOKEN_URL]
    assert len(token_calls) == 2
    assert "refresh_token=refresh-token-0" in token_calls[0].request.body  # stale cached
    assert "refresh_token=refresh-token-1" in token_calls[1].request.body  # re-read + retry
    # The successful exchange's rotation is persisted.
    assert secrets.store["refresh_token"] == "refresh-token-2"


@responses.activate
def test_refresh_400_with_same_stored_token_raises_without_retry():
    # Stored token == the one that failed → genuinely dead (needs re-auth);
    # no blind retry loop.
    responses.add(responses.POST, TOKEN_URL, json={"error": "invalid_grant"}, status=400)

    secrets = FakeSecretsClient(_secret())
    with pytest.raises(HttpError):
        QuickBooksConnector(secrets=secrets).get_company_info()

    token_calls = [c for c in responses.calls if c.request.url == TOKEN_URL]
    assert len(token_calls) == 1  # exactly one attempt


@responses.activate
def test_rotation_put_skipped_when_store_holds_newer_token():
    # Between our exchange and our write, a concurrent invocation stored a token
    # from a different exchange chain — clobbering it could persist a dead token.
    _add_token(refresh_token="refresh-token-OURS")
    responses.add(
        responses.GET,
        f"{SANDBOX_BASE}/v3/company/{REALM}/companyinfo/{REALM}",
        json={"CompanyInfo": {"CompanyName": "Aphrodite"}},
        status=200,
    )

    class RacingSecrets(FakeSecretsClient):
        def get(self, name: str, *, refresh: bool = False) -> dict[str, Any]:
            # Simulate another Lambda writing between our exchange and our put:
            # the pre-put refresh=True re-read sees a third token.
            if refresh:
                self.store["refresh_token"] = "refresh-token-THEIRS"
            return super().get(name, refresh=refresh)

    racing = RacingSecrets(_secret())
    QuickBooksConnector(secrets=racing).get_company_info()

    assert racing.puts == []  # ours was superseded → no clobbering write
    assert racing.store["refresh_token"] == "refresh-token-THEIRS"
