"""Contract tests for the SharePoint write surface (SharePointPublisher).

No network/AWS: HTTP stubbed with ``responses``, credentials from a fake secret.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import responses

from backend.connectors.graph.publisher import SharePointPublisher, sanitize_name
from services.http import HttpError

TENANT_ID = "tenant-123"
TOKEN_URL = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
BASE = "https://graph.microsoft.com/v1.0"


class FakeSecretsClient:
    def __init__(self, secret: dict[str, Any] | None = None) -> None:
        self._secret = secret or {
            "tenant_id": TENANT_ID,
            "client_id": "client-abc",
            "client_secret": "shh",
        }

    def get(self, name: str, *, refresh: bool = False) -> dict[str, Any]:
        return self._secret


def _add_token(token: str = "tok-1", expires_in: int = 3600) -> None:
    responses.add(
        responses.POST,
        TOKEN_URL,
        json={"access_token": token, "expires_in": expires_in, "token_type": "Bearer"},
        status=200,
    )


def _make_publisher() -> SharePointPublisher:
    return SharePointPublisher(FakeSecretsClient())


def test_sanitize_name_strips_illegal_chars() -> None:
    assert sanitize_name("Jane/Doe: Trust?") == "Jane Doe Trust"
    assert sanitize_name("  ..  ") == "unnamed"
    assert sanitize_name("Émile <Renaud>") == "Émile Renaud"


@responses.activate
def test_resolve_site_id() -> None:
    _add_token()
    responses.add(
        responses.GET,
        f"{BASE}/sites/aphrodite.sharepoint.com:/sites/AphroditeInternalSite",
        json={"id": "site-99"},
        status=200,
    )
    site_id = _make_publisher().resolve_site_id(
        "https://aphrodite.sharepoint.com/sites/AphroditeInternalSite"
    )
    assert site_id == "site-99"


@responses.activate
def test_get_item_by_path() -> None:
    _add_token()
    responses.add(
        responses.GET,
        f"{BASE}/sites/s1/drive/root:/Office%20Administration/Quarterly%20Reports",
        json={"id": "root-item", "webUrl": "https://sp/.../Quarterly%20Reports"},
        status=200,
    )
    item = _make_publisher().get_item_by_path("s1", "Office Administration/Quarterly Reports")
    assert item["id"] == "root-item"


@responses.activate
def test_ensure_child_folder_returns_existing_without_creating() -> None:
    _add_token()
    responses.add(
        responses.GET,
        f"{BASE}/sites/s1/drive/items/root1:/2026-Q2",
        json={"id": "q2", "folder": {}},
        status=200,
    )
    item = _make_publisher().ensure_child_folder("s1", "root1", "2026-Q2")
    assert item["id"] == "q2"
    # Only the token POST fired — no folder-creation POST to /children.
    assert not any("children" in c.request.url for c in responses.calls)


@responses.activate
def test_ensure_child_folder_creates_when_absent() -> None:
    _add_token()
    responses.add(
        responses.GET,
        f"{BASE}/sites/s1/drive/items/root1:/2026-Q2",
        json={"error": {"code": "itemNotFound"}},
        status=404,
    )
    responses.add(
        responses.POST,
        f"{BASE}/sites/s1/drive/items/root1/children",
        json={"id": "q2-new", "folder": {}},
        status=201,
    )
    item = _make_publisher().ensure_child_folder("s1", "root1", "2026-Q2")
    assert item["id"] == "q2-new"
    post = next(c for c in responses.calls if "children" in c.request.url)
    body = json.loads(post.request.body)
    assert body["name"] == "2026-Q2"
    assert "folder" in body
    assert body["@microsoft.graph.conflictBehavior"] == "fail"


@responses.activate
def test_upload_file_puts_content_with_type() -> None:
    _add_token()
    responses.add(
        responses.PUT,
        f"{BASE}/sites/s1/drive/items/p1:/cover.docx:/content",
        json={"id": "item-1", "webUrl": "https://sp/.../cover.docx"},
        status=201,
    )
    item = _make_publisher().upload_file(
        "s1", "p1", "cover.docx", b"DOCXBYTES", "application/octet-stream"
    )
    assert item["webUrl"].endswith("cover.docx")
    req = responses.calls[-1].request
    assert req.method == "PUT"
    assert req.body == b"DOCXBYTES"
    assert req.headers["Content-Type"] == "application/octet-stream"


@responses.activate
def test_write_refreshes_token_once_on_401() -> None:
    _add_token(token="tok-1")
    _add_token(token="tok-2")
    url = f"{BASE}/sites/s1/drive/items/p1:/cover.docx:/content"
    responses.add(responses.PUT, url, json={"error": "unauthorized"}, status=401)
    responses.add(responses.PUT, url, json={"id": "item-1"}, status=200)

    _make_publisher().upload_file("s1", "p1", "cover.docx", b"X", "application/octet-stream")

    token_calls = [c for c in responses.calls if c.request.url == TOKEN_URL]
    assert len(token_calls) == 2
    puts = [c for c in responses.calls if c.request.method == "PUT"]
    assert puts[-1].request.headers["Authorization"] == "Bearer tok-2"


@responses.activate
def test_error_raises_httperror_with_status() -> None:
    _add_token()
    responses.add(
        responses.GET,
        f"{BASE}/sites/s1/drive/root:/nope",
        json={"error": "forbidden"},
        status=403,
    )
    with pytest.raises(HttpError) as exc:
        _make_publisher().get_item_by_path("s1", "nope")
    assert "403" in str(exc.value)
