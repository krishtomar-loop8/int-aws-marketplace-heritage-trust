"""Contract tests for the read-only Clio Manage API v4 connector.

These exercise request shape (including the ``fields`` param), response parsing,
the OAuth refresh-token flow, ``meta.paging.next`` pagination, and the 401
recovery path. No network, AWS, or real credentials are touched: HTTP is faked
with the ``responses`` library and secrets come from a stub.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
import responses

from backend.connectors.clio.connector import (
    DEFAULT_BASE_URL,
    ClioConnector,
)
from services.http import HttpClient, HttpError

# Token endpoint derived from the (US) default base URL's origin. The connector
# pins the token host to the base URL's region; tests assert against that.
TOKEN_URL = "https://app.clio.com/oauth/token"


class FakeSecretsClient:
    """Minimal stand-in for ``services.secrets.SecretsClient``.

    ``put`` records durable writes so tests can distinguish a real Secrets Manager
    write (rotation persisted for cold starts) from an in-memory cache mutation.
    """

    def __init__(self, secret: dict[str, Any]) -> None:
        self._secret = secret
        self.puts: list[dict[str, Any]] = []

    def get(self, name: str, *, refresh: bool = False) -> dict[str, Any]:
        return self._secret

    def put(self, name: str, secret: dict[str, Any]) -> None:
        self._secret = dict(secret)
        self.puts.append(self._secret)


CREDS = {
    "client_id": "cid-123",
    "client_secret": "csecret-456",
    "refresh_token": "rtok-789",
}


def make_connector(secret: dict[str, Any] | None = None) -> tuple[ClioConnector, FakeSecretsClient]:
    """Build a connector with a no-rate-limit HttpClient so tests never sleep."""
    secrets = FakeSecretsClient(dict(secret or CREDS))
    connector = ClioConnector(secrets=secrets, http=HttpClient())
    return connector, secrets


def register_token(access_token: str = "access-abc", **extra: Any) -> None:
    body = {"access_token": access_token, "token_type": "bearer", "expires_in": 2_592_000}
    body.update(extra)
    responses.add(responses.POST, TOKEN_URL, json=body, status=200)


# -- OAuth refresh-token flow ---------------------------------------------


@responses.activate
def test_refresh_token_request_shape_and_bearer_header() -> None:
    register_token(access_token="access-abc")
    responses.add(
        responses.GET,
        f"{DEFAULT_BASE_URL}/matters.json",
        json={"data": [{"id": 1}], "meta": {"paging": {}}},
        status=200,
    )

    connector, _ = make_connector()
    connector.search_matters("Smith")

    token_call = responses.calls[0].request
    assert token_call.method == "POST"
    assert token_call.url == TOKEN_URL
    sent = parse_qs(token_call.body)
    assert sent["grant_type"] == ["refresh_token"]
    assert sent["client_id"] == ["cid-123"]
    assert sent["client_secret"] == ["csecret-456"]
    assert sent["refresh_token"] == ["rtok-789"]

    matters_call = responses.calls[1].request
    assert matters_call.headers["Authorization"] == "Bearer access-abc"


@responses.activate
def test_token_is_reused_across_calls() -> None:
    register_token()
    responses.add(
        responses.GET,
        f"{DEFAULT_BASE_URL}/bank_accounts.json",
        json={"data": [], "meta": {"paging": {}}},
        status=200,
    )

    connector, _ = make_connector()
    connector.list_bank_accounts()
    connector.list_bank_accounts()

    token_calls = [c for c in responses.calls if c.request.url == TOKEN_URL]
    assert len(token_calls) == 1


@responses.activate
def test_rotated_refresh_token_is_persisted() -> None:
    register_token(refresh_token="rtok-rotated")
    responses.add(
        responses.GET,
        f"{DEFAULT_BASE_URL}/bank_accounts.json",
        json={"data": [], "meta": {"paging": {}}},
        status=200,
    )

    connector, secrets = make_connector()
    connector.list_bank_accounts()

    # Must be durably written back to Secrets Manager (survives a cold start), not
    # only mutated in this process's cache.
    assert secrets.puts, "rotated refresh token must be persisted via put_secret_value"
    assert secrets.puts[-1]["refresh_token"] == "rtok-rotated"
    assert secrets.get("aphrodite/clio")["refresh_token"] == "rtok-rotated"


@responses.activate
def test_get_matter_account_balances_requests_nested_field_and_unwraps() -> None:
    register_token()
    responses.add(
        responses.GET,
        f"{DEFAULT_BASE_URL}/matters/42.json",
        json={
            "data": {
                "id": 42,
                "account_balances": [
                    {"id": 11, "name": "Main Trust", "type": "Trust", "balance": 38446.24}
                ],
            }
        },
        status=200,
    )

    connector, _ = make_connector()
    balances = connector.get_matter_account_balances(42)

    assert balances == [{"id": 11, "name": "Main Trust", "type": "Trust", "balance": 38446.24}]
    api_call = next(c.request for c in responses.calls if "/matters/42" in c.request.url)
    # url-encoded fields=id,account_balances{id,name,type,balance}
    assert "account_balances%7Bid%2Cname%2Ctype%2Cbalance%7D" in api_call.url


@responses.activate
def test_get_matter_account_balances_empty_when_absent() -> None:
    register_token()
    responses.add(
        responses.GET,
        f"{DEFAULT_BASE_URL}/matters/42.json",
        json={"data": {"id": 42}},  # no account_balances key
        status=200,
    )
    connector, _ = make_connector()
    assert connector.get_matter_account_balances(42) == []


# -- read methods: request shape (incl. fields) + parsing -----------------


@responses.activate
def test_search_matters_sends_fields_and_query() -> None:
    register_token()
    responses.add(
        responses.GET,
        f"{DEFAULT_BASE_URL}/matters.json",
        json={
            "data": [{"id": 42, "display_number": "00042-Smith"}],
            "meta": {"paging": {}},
        },
        status=200,
    )

    connector, _ = make_connector()
    matters = connector.search_matters("Smith", fields="id,display_number,client{name}")

    assert matters == [{"id": 42, "display_number": "00042-Smith"}]
    qs = parse_qs(urlparse(responses.calls[1].request.url).query)
    assert qs["query"] == ["Smith"]
    assert qs["fields"] == ["id,display_number,client{name}"]
    assert qs["limit"] == ["200"]


@responses.activate
def test_list_matters_sends_status_and_caps_results() -> None:
    register_token()
    responses.add(
        responses.GET,
        f"{DEFAULT_BASE_URL}/matters.json",
        json={
            "data": [{"id": 1}, {"id": 2}, {"id": 3}],
            "meta": {"paging": {}},
        },
        status=200,
    )

    connector, _ = make_connector()
    matters = connector.list_matters(fields="id,display_number", limit=2)

    # Capped at the limit even though the page held 3.
    assert [m["id"] for m in matters] == [1, 2]
    qs = parse_qs(urlparse(responses.calls[1].request.url).query)
    assert qs["status"] == ["open"]
    assert qs["fields"] == ["id,display_number"]
    assert "query" not in qs  # list, not search


@responses.activate
def test_get_matter_parses_data_envelope_with_fields() -> None:
    register_token()
    responses.add(
        responses.GET,
        f"{DEFAULT_BASE_URL}/matters/42.json",
        json={"data": {"id": 42, "status": "open"}},
        status=200,
    )

    connector, _ = make_connector()
    matter = connector.get_matter(42, fields="id,status")

    assert matter == {"id": 42, "status": "open"}
    qs = parse_qs(urlparse(responses.calls[1].request.url).query)
    assert qs["fields"] == ["id,status"]


@responses.activate
def test_list_matter_notes_requires_matter_type() -> None:
    register_token()
    responses.add(
        responses.GET,
        f"{DEFAULT_BASE_URL}/notes.json",
        json={"data": [{"id": 7, "detail": "call"}], "meta": {"paging": {}}},
        status=200,
    )

    connector, _ = make_connector()
    notes = connector.list_matter_notes(42)

    assert notes == [{"id": 7, "detail": "call"}]
    qs = parse_qs(urlparse(responses.calls[1].request.url).query)
    assert qs["matter_id"] == ["42"]
    assert qs["type"] == ["Matter"]


@responses.activate
def test_list_relationships_scopes_to_matter_and_hydrates_contact() -> None:
    register_token()
    responses.add(
        responses.GET,
        f"{DEFAULT_BASE_URL}/relationships.json",
        json={
            "data": [{"id": 3, "description": "Trustee", "contact": {"id": 8, "name": "Pat Roe"}}],
            "meta": {"paging": {}},
        },
        status=200,
    )

    connector, _ = make_connector()
    rels = connector.list_relationships(42, fields="id,description,contact{id,name}")

    assert rels == [{"id": 3, "description": "Trustee", "contact": {"id": 8, "name": "Pat Roe"}}]
    qs = parse_qs(urlparse(responses.calls[1].request.url).query)
    assert qs["matter_id"] == ["42"]
    assert qs["fields"] == ["id,description,contact{id,name}"]


# -- pagination via meta.paging.next --------------------------------------


@responses.activate
def test_pagination_follows_meta_paging_next() -> None:
    register_token()
    next_url = f"{DEFAULT_BASE_URL}/matters.json?page_token=PAGE2"
    responses.add(
        responses.GET,
        f"{DEFAULT_BASE_URL}/matters.json",
        json={"data": [{"id": 1}], "meta": {"paging": {"next": next_url}}},
        status=200,
    )
    responses.add(
        responses.GET,
        next_url,
        json={"data": [{"id": 2}], "meta": {"paging": {}}},
        status=200,
    )

    connector, _ = make_connector()
    matters = connector.search_matters("anything")

    assert [m["id"] for m in matters] == [1, 2]
    get_calls = [c for c in responses.calls if c.request.method == "GET"]
    assert len(get_calls) == 2


# -- error / 401 path ------------------------------------------------------


@responses.activate
def test_401_triggers_refresh_and_retry() -> None:
    register_token(access_token="stale-token")
    register_token(access_token="fresh-token")
    responses.add(
        responses.GET,
        f"{DEFAULT_BASE_URL}/matters/42.json",
        json={"error": "unauthorized"},
        status=401,
    )
    responses.add(
        responses.GET,
        f"{DEFAULT_BASE_URL}/matters/42.json",
        json={"data": {"id": 42}},
        status=200,
    )

    connector, _ = make_connector()
    matter = connector.get_matter(42)

    assert matter == {"id": 42}
    token_calls = [c for c in responses.calls if c.request.url == TOKEN_URL]
    assert len(token_calls) == 2
    get_calls = [c for c in responses.calls if c.request.method == "GET"]
    assert get_calls[-1].request.headers["Authorization"] == "Bearer fresh-token"


@responses.activate
def test_pagination_401_refreshes_and_retries() -> None:
    # The /matters incident: a warm container's cached access token goes stale,
    # so the first list page 401s. Paginated calls must recover like single gets —
    # force-refresh the token and retry the page once.
    register_token(access_token="stale-token")
    register_token(access_token="fresh-token")
    responses.add(
        responses.GET,
        f"{DEFAULT_BASE_URL}/matters.json",
        json={"error": "invalid_token"},
        status=401,
    )
    responses.add(
        responses.GET,
        f"{DEFAULT_BASE_URL}/matters.json",
        json={"data": [{"id": 1}], "meta": {"paging": {}}},
        status=200,
    )

    connector, _ = make_connector()
    matters = connector.search_matters("anything")

    assert [m["id"] for m in matters] == [1]
    token_calls = [c for c in responses.calls if c.request.url == TOKEN_URL]
    assert len(token_calls) == 2  # initial + forced refresh on the first-page 401
    get_calls = [c for c in responses.calls if c.request.method == "GET"]
    assert get_calls[-1].request.headers["Authorization"] == "Bearer fresh-token"


@responses.activate
def test_pagination_mid_walk_401_propagates_without_retry() -> None:
    # A 401 AFTER the first page must NOT restart pagination (that would
    # double-yield the first page); it propagates instead.
    register_token(access_token="t1")
    next_url = f"{DEFAULT_BASE_URL}/matters.json?page_token=P2"
    responses.add(
        responses.GET,
        f"{DEFAULT_BASE_URL}/matters.json",
        json={"data": [{"id": 1}], "meta": {"paging": {"next": next_url}}},
        status=200,
    )
    responses.add(responses.GET, next_url, json={"error": "invalid_token"}, status=401)

    connector, _ = make_connector()
    with pytest.raises(HttpError):
        connector.search_matters("anything")


@responses.activate
def test_token_refresh_failure_raises() -> None:
    responses.add(responses.POST, TOKEN_URL, json={"error": "invalid_grant"}, status=400)

    connector, _ = make_connector()
    with pytest.raises(HttpError):
        connector.list_bank_accounts()


@responses.activate
def test_persistent_401_propagates() -> None:
    register_token(access_token="t1")
    register_token(access_token="t2")
    responses.add(
        responses.GET,
        f"{DEFAULT_BASE_URL}/matters/42.json",
        json={"error": "unauthorized"},
        status=401,
    )
    responses.add(
        responses.GET,
        f"{DEFAULT_BASE_URL}/matters/42.json",
        json={"error": "unauthorized"},
        status=401,
    )

    connector, _ = make_connector()
    with pytest.raises(HttpError):
        connector.get_matter(42)


def test_missing_credentials_raise_value_error() -> None:
    connector, _ = make_connector({"client_id": "only-id"})
    with pytest.raises(ValueError):
        connector.list_bank_accounts()


# -- region resolution (US default, secret override, token-host lock-step) ----


def test_default_base_url_is_us_pod() -> None:
    # Defaults to the US pod; other-region accounts override via base_url.
    assert DEFAULT_BASE_URL == "https://app.clio.com/api/v4"


@responses.activate
def test_secret_base_url_overrides_region_and_token_host() -> None:
    """A CA-region secret retargets both the API and the OAuth token host."""
    ca_base = "https://ca.app.clio.com/api/v4"
    ca_token_url = "https://ca.app.clio.com/oauth/token"
    responses.add(
        responses.POST,
        ca_token_url,
        json={"access_token": "ca-tok", "token_type": "bearer", "expires_in": 2_592_000},
        status=200,
    )
    responses.add(
        responses.GET,
        f"{ca_base}/bank_accounts.json",
        json={"data": [], "meta": {"paging": {}}},
        status=200,
    )

    connector, _ = make_connector({**CREDS, "base_url": ca_base})
    assert connector.base_url == ca_base
    connector.list_bank_accounts()

    assert responses.calls[0].request.url == ca_token_url


@responses.activate
def test_constructor_base_url_takes_precedence_over_secret() -> None:
    override = "https://eu.app.clio.com/api/v4"
    responses.add(
        responses.POST,
        "https://eu.app.clio.com/oauth/token",
        json={"access_token": "eu-tok", "token_type": "bearer", "expires_in": 2_592_000},
        status=200,
    )
    responses.add(
        responses.GET,
        f"{override}/bank_accounts.json",
        json={"data": [], "meta": {"paging": {}}},
        status=200,
    )

    secrets = FakeSecretsClient({**CREDS, "base_url": "https://ca.app.clio.com/api/v4"})
    connector = ClioConnector(secrets=secrets, http=HttpClient(), base_url=override)
    assert connector.base_url == override
    connector.list_bank_accounts()
