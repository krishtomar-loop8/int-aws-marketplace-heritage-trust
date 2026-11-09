"""Contract tests for the read-only Estateably connector.

These mock HTTP with the ``responses`` library and stub Secrets Manager with a
small fake. No real network, AWS, or credentials are involved.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import responses

from backend.connectors.estateably.connector import (
    JSON_API_MEDIA_TYPE,
    USER_AGENT,
    EstateablyConnector,
)
from services.http import HttpClient, HttpError

BASE_URL = "https://app.test.estateably.com"
TOKEN = "jwt-token-abc"
TOKEN_2 = "jwt-token-def"


class FakeSecrets:
    """Minimal SecretsClient stand-in: exposes ``.get(name)`` returning a dict."""

    def __init__(self, secret: dict[str, Any]) -> None:
        self._secret = secret
        self.calls: list[str] = []
        self.get_calls: list[tuple[str, bool]] = []

    def get(self, name: str, *, refresh: bool = False) -> dict[str, Any]:
        self.calls.append(name)
        self.get_calls.append((name, refresh))
        return self._secret


def make_connector(secret: dict[str, Any] | None = None) -> EstateablyConnector:
    secret = secret or {
        "username": "ops@aphrodite.test",
        "password": "s3cret",
        "base_url": BASE_URL,
        "org_id": "org-123",
    }
    # No rate limiter / no sleeping so tests stay fast and deterministic.
    http = HttpClient(sleep=lambda _s: None)
    return EstateablyConnector(FakeSecrets(secret), http=http)


def add_login(token: str = TOKEN, status: int = 200) -> None:
    responses.add(
        responses.POST,
        f"{BASE_URL}/api/v1/auth/login",
        json={"data": {"type": "auth", "id": "u1", "attributes": {"token": token}}},
        status=status,
    )


# -- auth / JWT flow ---------------------------------------------------------


@responses.activate
def test_login_posts_json_api_credentials_and_uses_bearer_token():
    add_login()
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/estates/E1",
        json={"data": {"id": "E1", "type": "estate", "attributes": {"title": "Estate of A"}}},
        status=200,
    )

    estate = make_connector().get_estate("E1")
    assert estate["attributes"]["title"] == "Estate of A"

    login_call = responses.calls[0].request
    assert login_call.url == f"{BASE_URL}/api/v1/auth/login"
    assert login_call.method == "POST"
    assert login_call.headers["Content-Type"] == JSON_API_MEDIA_TYPE
    body = json.loads(login_call.body)
    assert body == {
        "data": {
            "type": "auth",
            "attributes": {"username": "ops@aphrodite.test", "password": "s3cret"},
        }
    }

    read_call = responses.calls[1].request
    assert read_call.headers["Authorization"] == f"Bearer {TOKEN}"
    assert read_call.headers["Accept"] == JSON_API_MEDIA_TYPE


@responses.activate
def test_login_caches_token_across_reads():
    add_login()
    for path in ("estates/E1", "estates/E1/summary"):
        responses.add(
            responses.GET,
            f"{BASE_URL}/api/v1/{path}",
            json={"data": {"id": "E1", "attributes": {}}},
            status=200,
        )

    conn = make_connector()
    conn.get_estate("E1")
    conn.get_estate_summary("E1")

    login_posts = [c for c in responses.calls if c.request.method == "POST"]
    assert len(login_posts) == 1


@responses.activate
def test_login_failure_raises():
    add_login(status=403)
    with pytest.raises(HttpError):
        make_connector().get_estate("E1")


@responses.activate
def test_login_without_token_in_response_raises():
    responses.add(
        responses.POST,
        f"{BASE_URL}/api/v1/auth/login",
        json={"data": {"attributes": {}}},
        status=200,
    )
    with pytest.raises(HttpError):
        make_connector().get_estate("E1")


def test_missing_credentials_raises():
    conn = make_connector({"base_url": BASE_URL})
    with pytest.raises(ValueError):
        conn.get_estate("E1")


# -- representative reads (request shape + response parsing) -----------------


@responses.activate
def test_get_estate_summary_parses_single_resource():
    add_login()
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/estates/E1/summary",
        json={
            "data": {
                "id": "E1",
                "type": "estateSummary",
                "attributes": {"status": "OPEN", "stage": "Administration"},
            }
        },
        status=200,
    )
    summary = make_connector().get_estate_summary("E1")
    assert summary["attributes"]["status"] == "OPEN"


@responses.activate
def test_list_contacts_returns_data_items():
    add_login()
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/estates/E1/contacts",
        json={
            "data": [
                {"id": "c1", "attributes": {"role": "executor", "name": "Jane"}},
                {"id": "c2", "attributes": {"role": "beneficiary", "name": "John"}},
            ],
            "meta": {"page": {"count": 1}, "items": {"count": 2}},
        },
        status=200,
    )
    contacts = make_connector().list_contacts("E1")
    assert [c["id"] for c in contacts] == ["c1", "c2"]
    # Estate sub-resources are a single un-paginated GET (page params 400 on some).
    read = next(c for c in responses.calls if c.request.method == "GET")
    assert "page%5B" not in read.request.url


@responses.activate
def test_list_substitute_decision_makers_returns_data_items():
    add_login()
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/estates/E1/substituteDecisionMakers",
        json={
            "data": [
                {"id": "s1", "attributes": {"name": "Pat Attorney", "dateOfBirth": "1962-04-01"}},
            ],
            "meta": {"page": {"count": 1}, "items": {"count": 1}},
        },
        status=200,
    )
    sdms = make_connector().list_substitute_decision_makers("E1")
    assert [s["id"] for s in sdms] == ["s1"]
    assert sdms[0]["attributes"]["dateOfBirth"] == "1962-04-01"


@responses.activate
def test_list_tasks_sends_estate_filter():
    add_login()
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/tasks",
        json={
            "data": [{"id": "t1", "attributes": {"name": "File T3"}}],
            "meta": {"page": {"count": 1}},
        },
        status=200,
    )
    tasks = make_connector().list_tasks("E1")
    assert tasks[0]["id"] == "t1"

    read_call = next(c for c in responses.calls if c.request.method == "GET")
    assert "filter%5BestateId%5D=E1" in read_call.request.url


@responses.activate
def test_global_search_sends_org_header_and_search_param():
    add_login()
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/globalSearch",
        json={
            "data": [{"id": "E9", "type": "estate", "attributes": {"title": "Smith Estate"}}],
            "meta": {"page": {"count": 1}},
        },
        status=200,
    )
    results = make_connector().global_search("Smith")
    assert results[0]["attributes"]["title"] == "Smith Estate"

    read_call = next(c for c in responses.calls if c.request.method == "GET")
    assert read_call.request.headers["orgId"] == "org-123"
    assert "search=Smith" in read_call.request.url


# -- pagination (page[number] / page[size]) — used by the top-level estates list


@responses.activate
def test_paginate_walks_pages_until_page_count():
    add_login()
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/estates",
        json={
            "data": [{"id": "e1", "attributes": {}}, {"id": "e2", "attributes": {}}],
            "meta": {"page": {"count": 2, "number": 1, "size": 2}, "items": {"count": 3}},
        },
        status=200,
    )
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/estates",
        json={
            "data": [{"id": "e3", "attributes": {}}],
            "meta": {"page": {"count": 2, "number": 2, "size": 2}, "items": {"count": 3}},
        },
        status=200,
    )

    estates = make_connector().list_estates()
    assert [e["id"] for e in estates] == ["e1", "e2", "e3"]

    gets = [c for c in responses.calls if c.request.method == "GET"]
    assert len(gets) == 2
    assert "page%5Bnumber%5D=1" in gets[0].request.url
    assert "page%5Bnumber%5D=2" in gets[1].request.url


@responses.activate
def test_paginate_stops_when_page_does_not_advance():
    # A collection that ignores page params and returns the same ids forever
    # (as estateably 'contacts' does) must not loop: stop on no new ids.
    add_login()
    same_page = {
        "data": [{"id": "e1", "attributes": {}}],
        "meta": {},  # no page.count reported
    }
    for _ in range(3):
        responses.add(responses.GET, f"{BASE_URL}/api/v1/estates", json=same_page, status=200)

    estates = make_connector().list_estates()
    assert [e["id"] for e in estates] == ["e1"]  # deduped, not repeated
    gets = [c for c in responses.calls if c.request.method == "GET"]
    assert len(gets) == 2  # page 1 (new) + page 2 (no progress → stop)


# -- estate sub-resources: single un-paginated GET ---------------------------


@responses.activate
def test_subresources_single_get_no_page_params():
    add_login()
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/estates/E1/beneficiaries",
        json={"data": [{"id": "b1"}, {"id": "b2"}, {"id": "b3"}], "meta": {"page": {"count": 1}}},
        status=200,
    )
    beneficiaries = make_connector().list_beneficiaries("E1")
    assert [b["id"] for b in beneficiaries] == ["b1", "b2", "b3"]
    gets = [c for c in responses.calls if c.request.method == "GET"]
    assert len(gets) == 1  # one call, no pagination
    assert "page%5B" not in gets[0].request.url  # no page params (would 400)


@responses.activate
def test_get_client_intake_uses_intake_path():
    add_login()
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/estates/E1/intake",
        json={"data": {"id": "E1", "attributes": {"birthDate": "1926-10-17"}}},
        status=200,
    )
    intake = make_connector().get_client_intake("E1")
    assert intake["attributes"]["birthDate"] == "1926-10-17"
    read = next(c for c in responses.calls if c.request.method == "GET")
    assert read.request.url.endswith("/estates/E1/intake")  # not 'clientIntake'


@responses.activate
def test_get_workflow_and_list_phases_use_workflow_endpoint():
    add_login()
    nodes = [{"name": "Intake"}, {"name": "Tax"}]
    workflow = {"data": {"id": "wf1", "attributes": {"nodes": nodes}}}
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/estates/E1/workflow",
        json=workflow,
        status=200,
    )
    conn = make_connector()
    assert conn.get_workflow("E1")["attributes"]["nodes"][0]["name"] == "Intake"
    assert [n["name"] for n in conn.list_phases("E1")] == ["Intake", "Tax"]


# -- document binary download ------------------------------------------------


@responses.activate
def test_download_document_returns_raw_bytes():
    add_login()
    pdf_bytes = b"%PDF-1.7 binary content"
    # The raw-file endpoint (not filePreview) — returns the actual binary.
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/documents/D1/file",
        body=pdf_bytes,
        status=200,
        content_type="application/octet-stream",
    )

    content = make_connector().download_document("D1")
    assert content == pdf_bytes

    read_call = next(c for c in responses.calls if c.request.method == "GET")
    assert read_call.request.url.endswith("/documents/D1/file")
    assert read_call.request.headers["Authorization"] == f"Bearer {TOKEN}"


def test_is_downloadable_gates_folders_and_fileless_rows():
    from backend.connectors.estateably.connector import EstateablyConnector

    def doc(type_, file):
        return {"type": "documents", "id": "x", "attributes": {"type": type_, "file": file}}

    # a real file
    assert EstateablyConnector.is_downloadable(
        doc("support", {"originalname": "will.pdf", "mimetype": "application/pdf"})
    )
    # a folder row → never downloadable
    assert not EstateablyConnector.is_downloadable(doc("folder", None))
    # a structured/workflow row with no attached file → skip
    assert not EstateablyConnector.is_downloadable(doc("workflow", None))
    # accepts a bare attributes dict too
    assert EstateablyConnector.is_downloadable({"type": "support", "file": {"originalname": "a"}})


# -- 401-then-reauth path ----------------------------------------------------


@responses.activate
def test_expired_token_triggers_reauth_and_retry():
    # First login, then an expired-token 401, then a re-login, then success.
    add_login(token=TOKEN)
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/estates/E1",
        json={"errors": [{"status": "401", "title": "Unable to authenticate the user"}]},
        status=401,
    )
    add_login(token=TOKEN_2)
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/estates/E1",
        json={"data": {"id": "E1", "attributes": {"title": "Recovered"}}},
        status=200,
    )

    estate = make_connector().get_estate("E1")
    assert estate["attributes"]["title"] == "Recovered"

    post_calls = [c for c in responses.calls if c.request.method == "POST"]
    assert len(post_calls) == 2  # initial login + re-auth

    final_get = [c for c in responses.calls if c.request.method == "GET"][-1]
    assert final_get.request.headers["Authorization"] == f"Bearer {TOKEN_2}"


@responses.activate
def test_login_itself_retries_with_fresh_secret_on_stale_cached_credentials():
    # A container that has NEVER successfully logged in yet (self._token is
    # None — e.g. a cold start right after an admin reconnect) goes straight
    # to _login() with no refresh, not through _get_response's 401 retry.
    # If the cached secret is stale, _login must self-heal rather than fail
    # outright, since the retry logic added for the reconnect scenario only
    # lived in _get_response's handler, not in _login itself.
    secrets = FakeSecrets({"username": "old", "password": "old-pw", "base_url": BASE_URL})
    http = HttpClient(sleep=lambda _s: None)
    connector = EstateablyConnector(secrets, http=http)

    add_login(status=401)  # stale cached creds rejected
    secrets._secret = {"username": "new", "password": "new-pw", "base_url": BASE_URL}  # admin reconnected
    add_login(token=TOKEN_2)  # only matches once the retry reads the NEW secret
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/estates/E1",
        json={"data": {"id": "E1", "attributes": {"title": "Recovered"}}},
        status=200,
    )

    estate = connector.get_estate("E1")

    assert estate["attributes"]["title"] == "Recovered"
    assert secrets.get_calls[-1] == ("aphrodite/estateably", True)


@responses.activate
def test_401_after_reconnect_uses_fresh_secret_not_cache():
    # Simulates: container warm with old creds, admin reconnects (secret
    # changes in Secrets Manager), next call 401s on the stale cached creds,
    # and the retry must re-read the secret rather than reusing the cache.
    secrets = FakeSecrets({"username": "old", "password": "old-pw", "base_url": BASE_URL})
    http = HttpClient(sleep=lambda _s: None)
    connector = EstateablyConnector(secrets, http=http)
    add_login(token=TOKEN)
    connector._login()

    secrets._secret = {"username": "new", "password": "new-pw", "base_url": BASE_URL}
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/globalSearch",
        json={"errors": [{"status": "401"}]},
        status=401,
    )
    add_login(token=TOKEN_2)  # only matches if the retry used the NEW creds
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/globalSearch",
        json={"data": []},
        status=200,
    )

    connector.global_search("x")

    assert secrets.get_calls[-1] == ("aphrodite/estateably", True)


@responses.activate
def test_non_401_error_is_not_retried():
    add_login()
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/estates/E1",
        json={"errors": [{"status": "404"}]},
        status=404,
    )
    with pytest.raises(HttpError):
        make_connector().get_estate("E1")

    gets = [c for c in responses.calls if c.request.method == "GET"]
    assert len(gets) == 1  # no retry on a 404


@responses.activate
def test_non_401_status_is_not_reauthed_even_when_url_contains_401():
    # Latent-bug guard: re-auth must key off the structured HTTP status, not a
    # substring of the exception message (which embeds the URL). A 402 on a URL
    # that merely contains "401" (e.g. inside an opaque id) must NOT re-auth.
    estate_id = "6912401fffa62364db25f240"  # id happens to contain "401"
    add_login()
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/estates/{estate_id}/summary",
        json={"errors": [{"status": "402", "title": "Missing payment for case"}]},
        status=402,
    )
    with pytest.raises(HttpError) as ei:
        make_connector().get_estate_summary(estate_id)
    assert ei.value.status == 402

    post_calls = [c for c in responses.calls if c.request.method == "POST"]
    assert len(post_calls) == 1  # login once only — no spurious re-auth
    gets = [c for c in responses.calls if c.request.method == "GET"]
    assert len(gets) == 1  # no retry


# -- base_url resolution -----------------------------------------------------


@responses.activate
def test_base_url_override_takes_precedence_over_secret():
    override = "https://override.estateably.test"
    responses.add(
        responses.POST,
        f"{override}/api/v1/auth/login",
        json={"data": {"attributes": {"token": TOKEN}}},
        status=200,
    )
    responses.add(
        responses.GET,
        f"{override}/api/v1/estates/E1",
        json={"data": {"id": "E1", "attributes": {}}},
        status=200,
    )
    http = HttpClient(sleep=lambda _s: None)
    conn = EstateablyConnector(
        FakeSecrets({"username": "u", "password": "p", "base_url": BASE_URL}),
        http=http,
        base_url=override,
    )
    conn.get_estate("E1")
    assert conn.base_url == override


@responses.activate
def test_requests_send_user_agent():
    # Estateably's WAF 403s the default python-requests UA, so login and reads
    # must carry an explicit User-Agent.
    add_login()
    responses.add(
        responses.GET,
        f"{BASE_URL}/api/v1/estates",
        json={"data": [], "meta": {"page": {"count": 1}}},
        status=200,
    )
    make_connector().list_estates(page_size=1)
    assert responses.calls
    for call in responses.calls:
        assert call.request.headers["User-Agent"] == USER_AGENT
