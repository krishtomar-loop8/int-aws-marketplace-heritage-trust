"""Contract tests for the one-time Clio OAuth authorization-code bootstrap.

HTTP is faked with ``responses``; no network or real credentials are touched.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
import responses

from backend.connectors.clio.oauth_bootstrap import (
    DEFAULT_REDIRECT_URI,
    build_authorize_url,
    exchange_code,
)
from services.http import HttpClient, HttpError

US_TOKEN_URL = "https://app.clio.com/oauth/token"


# -- authorize URL ---------------------------------------------------------


def test_build_authorize_url_default_us_origin_and_params() -> None:
    url, state = build_authorize_url("cid-123")
    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == (
        "https://app.clio.com/oauth/authorize"
    )
    qs = parse_qs(parsed.query)
    assert qs["response_type"] == ["code"]
    assert qs["client_id"] == ["cid-123"]
    assert qs["redirect_uri"] == [DEFAULT_REDIRECT_URI]
    assert qs["state"] == [state]
    assert state  # non-empty, generated when not supplied
    assert "scope" not in qs  # Clio scopes are fixed on the app


def test_build_authorize_url_honours_state_and_region_override() -> None:
    url, state = build_authorize_url(
        "cid-123", base_url="https://ca.app.clio.com/api/v4", state="fixed-state"
    )
    assert state == "fixed-state"
    assert url.startswith("https://ca.app.clio.com/oauth/authorize?")


# -- code exchange ---------------------------------------------------------


@responses.activate
def test_exchange_code_request_shape_and_returns_refresh_token() -> None:
    responses.add(
        responses.POST,
        US_TOKEN_URL,
        json={
            "access_token": "acc-1",
            "refresh_token": "ref-1",
            "token_type": "bearer",
            "expires_in": 2_592_000,
        },
        status=200,
    )

    payload = exchange_code("cid-123", "csec-456", "the-code", http=HttpClient())
    assert payload["refresh_token"] == "ref-1"

    sent = parse_qs(responses.calls[0].request.body)
    assert sent["grant_type"] == ["authorization_code"]
    assert sent["client_id"] == ["cid-123"]
    assert sent["client_secret"] == ["csec-456"]
    assert sent["code"] == ["the-code"]
    assert sent["redirect_uri"] == [DEFAULT_REDIRECT_URI]


@responses.activate
def test_exchange_code_derives_token_host_from_base_url() -> None:
    responses.add(
        responses.POST,
        "https://ca.app.clio.com/oauth/token",
        json={"access_token": "a", "refresh_token": "r", "expires_in": 1},
        status=200,
    )
    exchange_code(
        "cid",
        "csec",
        "code",
        base_url="https://ca.app.clio.com/api/v4",
        http=HttpClient(),
    )
    assert responses.calls[0].request.url == "https://ca.app.clio.com/oauth/token"


@responses.activate
def test_exchange_code_raises_on_http_error() -> None:
    responses.add(responses.POST, US_TOKEN_URL, json={"error": "invalid_grant"}, status=400)
    with pytest.raises(HttpError):
        exchange_code("cid", "csec", "bad-code", http=HttpClient())


@responses.activate
def test_exchange_code_raises_when_no_refresh_token() -> None:
    responses.add(
        responses.POST,
        US_TOKEN_URL,
        json={"access_token": "acc-only", "expires_in": 1},
        status=200,
    )
    with pytest.raises(HttpError):
        exchange_code("cid", "csec", "code", http=HttpClient())
