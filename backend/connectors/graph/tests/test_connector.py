"""Contract tests for the read-only Microsoft Graph connector.

No network, AWS, or real credentials: HTTP is stubbed with ``responses`` and a
fake SecretsClient supplies app-only credentials.
"""

from __future__ import annotations

from typing import Any

import pytest
import responses

from backend.connectors.graph import GraphConnector
from services.http import HttpError

TENANT_ID = "tenant-123"
TOKEN_URL = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
BASE = "https://graph.microsoft.com/v1.0"


class FakeSecretsClient:
    """Minimal SecretsClient stub: returns a fixed Graph credential dict."""

    def __init__(self, secret: dict[str, Any] | None = None) -> None:
        self._secret = secret or {
            "tenant_id": TENANT_ID,
            "client_id": "client-abc",
            "client_secret": "shh",
        }
        self.calls = 0

    def get(self, name: str, *, refresh: bool = False) -> dict[str, Any]:
        self.calls += 1
        return self._secret


def _add_token(expires_in: int = 3600, token: str = "tok-1") -> None:
    responses.add(
        responses.POST,
        TOKEN_URL,
        json={"access_token": token, "expires_in": expires_in, "token_type": "Bearer"},
        status=200,
    )


def _make_connector() -> tuple[GraphConnector, FakeSecretsClient]:
    secrets = FakeSecretsClient()
    return GraphConnector(secrets), secrets


# -- token flow --------------------------------------------------------


@responses.activate
def test_client_credentials_token_flow() -> None:
    _add_token()
    responses.add(responses.GET, f"{BASE}/users/u1", json={"id": "u1"}, status=200)

    connector, secrets = _make_connector()
    user = connector.get_user("u1")

    assert user == {"id": "u1"}
    # Token request shape.
    token_req = responses.calls[0].request
    assert token_req.url == TOKEN_URL
    body = token_req.body
    assert "grant_type=client_credentials" in body
    assert "scope=https%3A%2F%2Fgraph.microsoft.com%2F.default" in body
    assert "client_id=client-abc" in body
    # Bearer header set on the data call.
    assert responses.calls[1].request.headers["Authorization"] == "Bearer tok-1"


@responses.activate
def test_token_is_cached_across_calls() -> None:
    _add_token()
    responses.add(responses.GET, f"{BASE}/users/u1", json={"id": "u1"}, status=200)
    responses.add(responses.GET, f"{BASE}/users/u2", json={"id": "u2"}, status=200)

    connector, _ = _make_connector()
    connector.get_user("u1")
    connector.get_user("u2")

    token_calls = [c for c in responses.calls if c.request.url == TOKEN_URL]
    assert len(token_calls) == 1


# -- reads: request shape + parsing ------------------------------------


@responses.activate
def test_list_messages_search_shape_and_parsing() -> None:
    _add_token()
    responses.add(
        responses.GET,
        f"{BASE}/users/u1/messages",
        json={"value": [{"id": "m1", "subject": "Smith Estate"}]},
        status=200,
    )

    connector, _ = _make_connector()
    msgs = connector.list_messages("u1", search="Smith", top=10)

    assert msgs == [{"id": "m1", "subject": "Smith Estate"}]
    req = responses.calls[1].request
    assert "%24search=%22Smith%22" in req.url
    assert "%24top=10" in req.url


@responses.activate
def test_search_mailbox_selects_recipients_for_exclusion_filter() -> None:
    _add_token()
    responses.add(
        responses.GET,
        f"{BASE}/users/u1/messages",
        json={"value": [{"id": "m1", "subject": "Smith Estate"}]},
        status=200,
    )

    connector, _ = _make_connector()
    msgs = connector.search_mailbox("u1", "Smith AND Estate", top=20)

    assert msgs == [{"id": "m1", "subject": "Smith Estate"}]
    req = responses.calls[1].request
    # to/ccRecipients must be selected so the fan-out can drop @loop8.io emails.
    assert "toRecipients" in req.url
    assert "ccRecipients" in req.url


@responses.activate
def test_list_calendar_events_parsing() -> None:
    _add_token()
    responses.add(
        responses.GET,
        f"{BASE}/users/u1/events",
        json={"value": [{"id": "e1", "subject": "Filing deadline"}]},
        status=200,
    )

    connector, _ = _make_connector()
    events = connector.list_calendar_events("u1")

    assert events == [{"id": "e1", "subject": "Filing deadline"}]
    assert responses.calls[1].request.url.startswith(f"{BASE}/users/u1/events")


@responses.activate
def test_get_site_resolves_hostname_and_path() -> None:
    _add_token()
    responses.add(
        responses.GET,
        f"{BASE}/sites/contoso.sharepoint.com:/sites/Matters",
        json={"id": "site-1"},
        status=200,
    )

    connector, _ = _make_connector()
    site = connector.get_site("contoso.sharepoint.com", "/sites/Matters")

    assert site == {"id": "site-1"}


# -- pagination via @odata.nextLink ------------------------------------


@responses.activate
def test_pagination_follows_next_link() -> None:
    _add_token()
    next_link = f"{BASE}/sites/site-1/drive/root/children?$skiptoken=abc"
    responses.add(
        responses.GET,
        f"{BASE}/sites/site-1/drive/root/children",
        json={"value": [{"id": "f1"}], "@odata.nextLink": next_link},
        status=200,
    )
    responses.add(
        responses.GET,
        next_link,
        json={"value": [{"id": "f2"}]},
        status=200,
    )

    connector, _ = _make_connector()
    items = connector.list_site_drive_items("site-1")

    assert [i["id"] for i in items] == ["f1", "f2"]


# -- error / 401 paths -------------------------------------------------


@responses.activate
def test_401_triggers_token_refresh_and_retry() -> None:
    _add_token(token="tok-1")
    # First data call: 401. Then a fresh token, then success.
    responses.add(responses.GET, f"{BASE}/users/u1", json={"error": "unauth"}, status=401)
    _add_token(token="tok-2")
    responses.add(responses.GET, f"{BASE}/users/u1", json={"id": "u1"}, status=200)

    connector, _ = _make_connector()
    user = connector.get_user("u1")

    assert user == {"id": "u1"}
    token_calls = [c for c in responses.calls if c.request.url == TOKEN_URL]
    assert len(token_calls) == 2
    # Retry used the refreshed bearer token.
    last_get = [c for c in responses.calls if c.request.url == f"{BASE}/users/u1"][-1]
    assert last_get.request.headers["Authorization"] == "Bearer tok-2"


@responses.activate
def test_404_raises_http_error() -> None:
    _add_token()
    responses.add(responses.GET, f"{BASE}/users/missing", json={"error": "nf"}, status=404)

    connector, _ = _make_connector()
    with pytest.raises(HttpError):
        connector.get_user("missing")


# -- crawler helpers: delta enumeration + content download -------------


@responses.activate
def test_list_drive_delta_paginates_and_returns_delta_link() -> None:
    _add_token()
    next_link = f"{BASE}/sites/site-1/drive/root/delta?$skiptoken=abc"
    delta_link = f"{BASE}/sites/site-1/drive/root/delta?token=NEXT"
    responses.add(
        responses.GET,
        f"{BASE}/sites/site-1/drive/root/delta",
        json={"value": [{"id": "a"}], "@odata.nextLink": next_link},
        status=200,
    )
    responses.add(
        responses.GET,
        next_link,
        json={"value": [{"id": "b"}], "@odata.deltaLink": delta_link},
        status=200,
    )

    connector, _ = _make_connector()
    items, delta = connector.list_drive_delta("site-1")

    assert [i["id"] for i in items] == ["a", "b"]
    assert delta == delta_link


@responses.activate
def test_list_drive_delta_uses_supplied_cursor() -> None:
    _add_token()
    cursor = f"{BASE}/sites/site-1/drive/root/delta?token=PREV"
    responses.add(responses.GET, cursor, json={"value": [{"id": "c"}]}, status=200)

    connector, _ = _make_connector()
    items, delta = connector.list_drive_delta("site-1", delta_link=cursor)

    assert [i["id"] for i in items] == ["c"]
    assert delta is None  # no deltaLink in this (final, single) page
    assert responses.calls[1].request.url == cursor  # went straight to the cursor


@responses.activate
def test_iter_drive_delta_streams_pages_with_resume_cursors() -> None:
    _add_token()
    next_link = f"{BASE}/sites/site-1/drive/root/delta?$skiptoken=abc"
    delta_link = f"{BASE}/sites/site-1/drive/root/delta?token=NEXT"
    responses.add(
        responses.GET,
        f"{BASE}/sites/site-1/drive/root/delta",
        json={"value": [{"id": "a"}], "@odata.nextLink": next_link},
        status=200,
    )
    responses.add(
        responses.GET,
        next_link,
        json={"value": [{"id": "b"}], "@odata.deltaLink": delta_link},
        status=200,
    )

    connector, _ = _make_connector()
    pages = list(connector.iter_drive_delta("site-1"))

    # one tuple per page; non-final page yields the nextLink, final yields deltaLink
    assert [([i["id"] for i in items], cursor) for items, cursor in pages] == [
        (["a"], next_link),
        (["b"], delta_link),
    ]


@responses.activate
def test_download_drive_item_returns_bytes() -> None:
    _add_token()
    responses.add(
        responses.GET,
        f"{BASE}/sites/site-1/drive/items/it-1/content",
        body=b"%PDF-1.7 binary",
        status=200,
    )

    connector, _ = _make_connector()
    data = connector.download_drive_item("site-1", "it-1")

    assert data == b"%PDF-1.7 binary"


@responses.activate
def test_search_uses_post_and_parses_value() -> None:
    _add_token()
    responses.add(
        responses.POST,
        f"{BASE}/search/query",
        json={"value": [{"hitsContainers": [{"hits": [{"hitId": "h1"}]}]}]},
        status=200,
    )

    connector, _ = _make_connector()
    results = connector.search("Smith Estate")

    assert results == [{"hitsContainers": [{"hits": [{"hitId": "h1"}]}]}]
    search_call = [c for c in responses.calls if c.request.url == f"{BASE}/search/query"][0]
    assert search_call.request.headers["Authorization"] == "Bearer tok-1"
