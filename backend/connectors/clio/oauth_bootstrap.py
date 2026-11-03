"""One-time OAuth authorization-code bootstrap for Clio.

The runtime :class:`~backend.connectors.clio.connector.ClioConnector` only ever
performs the ``refresh_token`` grant. Before it can do that, a customer user must
authorize the private app once and we must exchange the resulting authorization
code for a long-lived ``refresh_token``. That one-time exchange lives here (and
is driven by ``scripts/clio_oauth_bootstrap.py``) rather than in the connector,
which stays read-only at runtime.

Clio uses the out-of-band manual-approval flow here: the authorize URL
carries ``redirect_uri=https://app.clio.com/oauth/approval``; after the user
clicks *Allow*, Clio displays a short-lived authorization code on screen (it
expires in ~10 minutes) which is then exchanged here.

The OAuth origin is derived from the API base URL so the region stays in
lock-step with the connector (defaults to the US pod).
"""

from __future__ import annotations

import secrets as _secrets
from typing import Any
from urllib.parse import urlencode, urlsplit

from services.http import HttpClient, HttpError
from services.logging import get_logger

from .connector import DEFAULT_BASE_URL

logger = get_logger(__name__)

# The out-of-band manual-approval redirect: Clio shows the code on screen rather
# than redirecting to a callback we'd have to host.
DEFAULT_REDIRECT_URI = "https://app.clio.com/oauth/approval"


def _origin(base_url: str) -> str:
    """Return the scheme://host origin of an API base URL (drops the path)."""
    parts = urlsplit(base_url)
    return f"{parts.scheme}://{parts.netloc}"


def build_authorize_url(
    client_id: str,
    *,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    base_url: str = DEFAULT_BASE_URL,
    state: str | None = None,
) -> tuple[str, str]:
    """Build the Clio authorization URL and return ``(url, state)``.

    No ``scope`` parameter is sent — Clio derives the granted permissions from
    the private app's configured (read-only) scopes. ``state`` is generated when
    not supplied; for the OOB flow it is not server-validated but is included as
    good practice.
    """
    state = state or _secrets.token_urlsafe(24)
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"{_origin(base_url)}/oauth/authorize?{query}", state


def exchange_code(
    client_id: str,
    client_secret: str,
    code: str,
    *,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    base_url: str = DEFAULT_BASE_URL,
    http: HttpClient | None = None,
) -> dict[str, Any]:
    """Exchange an authorization ``code`` for tokens (authorization_code grant).

    Returns the raw token payload (``access_token``, ``refresh_token``,
    ``expires_in``, ...). Raises :class:`HttpError` on a non-2xx response or if
    the payload lacks a ``refresh_token`` (which the connector needs to persist).
    """
    http = http or HttpClient()
    token_url = f"{_origin(base_url)}/oauth/token"
    response = http.session.post(
        token_url,
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        },
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=http.timeout,
    )
    if response.status_code >= 400:
        # Never log the code/secret; the status is enough to diagnose.
        logger.error(
            "Clio authorization-code exchange failed",
            extra={"extra_fields": {"status": response.status_code}},
        )
        raise HttpError(f"Clio authorization-code exchange failed: {response.status_code}")

    payload = response.json()
    if not payload.get("refresh_token"):
        raise HttpError("Clio token exchange returned no refresh_token")
    return payload
