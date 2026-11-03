"""One-time OAuth authorization-code bootstrap for QuickBooks Online.

The runtime :class:`~backend.connectors.quickbooks.connector.QuickBooksConnector`
only performs the ``refresh_token`` grant. Before it can, a customer QuickBooks
admin must authorize the app once against the customer's production company; we then
exchange the returned authorization code for a long-lived ``refresh_token`` and
capture the ``realmId`` Intuit puts on the redirect (the company id the connector
reads from). That one-time exchange lives here (driven by
``scripts/quickbooks_oauth_bootstrap.py``) rather than in the connector, which
stays read-only at runtime.

Intuit has no out-of-band redirect like Clio's, so the authorize URL uses a real
HTTPS redirect we control; after consent the browser lands there with
``?code=…&realmId=…&state=…``, and those values are exchanged here.
"""

from __future__ import annotations

import base64
import secrets as _secrets
from typing import Any
from urllib.parse import urlencode

from services.http import HttpClient, HttpError
from services.logging import get_logger

logger = get_logger(__name__)

AUTHORIZE_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
# Intuit's Accounting API scope. It grants read AND write at Intuit's level (there
# is no read-only scope); the connector enforces read-only by only issuing GETs.
DEFAULT_SCOPE = "com.intuit.quickbooks.accounting"


def build_authorize_url(
    client_id: str,
    *,
    redirect_uri: str,
    scope: str = DEFAULT_SCOPE,
    state: str | None = None,
) -> tuple[str, str]:
    """Build the Intuit authorization URL and return ``(url, state)``.

    ``redirect_uri`` must exactly match one registered on the Intuit app. On
    approval Intuit redirects there with ``code``, ``realmId`` and ``state``.
    """
    state = state or _secrets.token_urlsafe(24)
    query = urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "scope": scope,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{query}", state


def exchange_code(
    client_id: str,
    client_secret: str,
    code: str,
    *,
    redirect_uri: str,
    http: HttpClient | None = None,
) -> dict[str, Any]:
    """Exchange an authorization ``code`` for tokens (authorization_code grant).

    Uses HTTP Basic auth (``client_id:client_secret``), matching the connector's
    refresh call. Returns the raw token payload (``access_token``,
    ``refresh_token``, ``expires_in``, ...). Raises :class:`HttpError` on a
    non-2xx response or if the payload lacks a ``refresh_token``.
    """
    http = http or HttpClient()
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    response = http.session.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        headers={
            "Authorization": f"Basic {basic}",
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=http.timeout,
    )
    if response.status_code >= 400:
        # Never log the code/secret; the status is enough to diagnose.
        logger.error(
            "QuickBooks authorization-code exchange failed",
            extra={"extra_fields": {"status": response.status_code}},
        )
        raise HttpError(f"QuickBooks authorization-code exchange failed: {response.status_code}")

    payload = response.json()
    if not payload.get("refresh_token"):
        raise HttpError("QuickBooks token exchange returned no refresh_token")
    return payload
