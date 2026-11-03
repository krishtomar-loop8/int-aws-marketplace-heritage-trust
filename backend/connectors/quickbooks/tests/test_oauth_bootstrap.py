"""Contract tests for the one-time QuickBooks OAuth authorization-code bootstrap.

HTTP is faked with ``responses``; no network or real credentials are touched.
"""

from __future__ import annotations

import base64
from urllib.parse import parse_qs, urlparse

import pytest
import responses

from backend.connectors.quickbooks.oauth_bootstrap import (
    AUTHORIZE_URL,
    DEFAULT_SCOPE,
    TOKEN_URL,
    build_authorize_url,
    exchange_code,
)
from services.http import HttpClient, HttpError

REDIRECT = "https://d1zumlm52l64zj.cloudfront.net/"


# -- authorize URL ---------------------------------------------------------


def test_build_authorize_url_params() -> None:
    url, state = build_authorize_url("cid-123", redirect_uri=REDIRECT)
    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == AUTHORIZE_URL
    qs = parse_qs(parsed.query)
    assert qs["response_type"] == ["code"]
    assert qs["client_id"] == ["cid-123"]
    assert qs["scope"] == [DEFAULT_SCOPE]
    assert qs["redirect_uri"] == [REDIRECT]
    assert qs["state"] == [state]
    assert state  # generated when not supplied


def test_build_authorize_url_honours_state() -> None:
    url, state = build_authorize_url("cid-123", redirect_uri=REDIRECT, state="fixed")
    assert state == "fixed"
    assert "state=fixed" in url


# -- code exchange ---------------------------------------------------------


@responses.activate
def test_exchange_code_uses_basic_auth_and_returns_refresh_token() -> None:
    responses.add(
        responses.POST,
        TOKEN_URL,
        json={
            "access_token": "acc-1",
            "refresh_token": "ref-1",
            "token_type": "bearer",
            "expires_in": 3600,
        },
        status=200,
    )

    payload = exchange_code(
        "cid-123", "csec-456", "the-code", redirect_uri=REDIRECT, http=HttpClient()
    )
    assert payload["refresh_token"] == "ref-1"

    req = responses.calls[0].request
    expected_basic = base64.b64encode(b"cid-123:csec-456").decode()
    assert req.headers["Authorization"] == f"Basic {expected_basic}"
    sent = parse_qs(req.body)
    assert sent["grant_type"] == ["authorization_code"]
    assert sent["code"] == ["the-code"]
    assert sent["redirect_uri"] == [REDIRECT]
    # secret goes in the Basic header, never the body
    assert "client_secret" not in sent


@responses.activate
def test_exchange_code_raises_on_error_status() -> None:
    responses.add(responses.POST, TOKEN_URL, json={"error": "invalid_grant"}, status=400)
    with pytest.raises(HttpError):
        exchange_code("cid", "csec", "bad-code", redirect_uri=REDIRECT)


@responses.activate
def test_exchange_code_raises_when_no_refresh_token() -> None:
    responses.add(
        responses.POST, TOKEN_URL, json={"access_token": "acc", "expires_in": 3600}, status=200
    )
    with pytest.raises(HttpError):
        exchange_code("cid", "csec", "the-code", redirect_uri=REDIRECT)
