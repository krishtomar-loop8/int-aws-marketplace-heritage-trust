"""Connector onboarding & reconnect: validate-then-store credential writes
for the admin Setup screen.

Each ``connect_*``/``*_exchange`` function validates the submitted
credential live against the source system before persisting it to Secrets
Manager — a failed validation leaves the existing secret untouched. Routes
calling into this module are admin-gated in ``handlers.py`` (see
``_require_admin``); this module itself does no authorization — it is pure
validate-then-store logic, unit-testable without an API Gateway event.
"""

from __future__ import annotations

from typing import Any

from backend.harness import run_check
from services.secrets import SecretsClient


class ConnectorValidationError(Exception):
    """A submitted credential failed live validation; nothing was stored."""


class _StaticSecrets:
    """A SecretsClient-shaped object serving one fixed, in-memory secret.

    Backs a throwaway connector instance used only to prove a submitted
    credential works before it is ever written to Secrets Manager.
    """

    def __init__(self, value: dict[str, Any]) -> None:
        self._value = value

    def get(self, name: str, *, refresh: bool = False) -> dict[str, Any]:
        return self._value


def connect_estateably(secrets: SecretsClient, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and store Estateably service-account credentials.

    Returns ``run_check("estateably", secrets)``'s result on success; raises
    :class:`ConnectorValidationError` (nothing stored) on a bad credential.
    """
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if not username or not password:
        raise ConnectorValidationError("username and password are required")

    candidate: dict[str, Any] = {"username": username, "password": password}
    base_url = str(payload.get("base_url") or "").strip()
    if base_url:
        candidate["base_url"] = base_url

    from backend.connectors.estateably.connector import EstateablyConnector

    connector = EstateablyConnector(_StaticSecrets(candidate))
    try:
        connector._login()
    except Exception as exc:  # noqa: BLE001 - surfaced as a clear validation error
        raise ConnectorValidationError(f"Estateably login failed: {exc}") from exc

    secrets.put("aphrodite/estateably", candidate)
    return run_check("estateably", secrets)


def connect_graph(secrets: SecretsClient, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and store Microsoft Graph app-only credentials.

    Returns ``run_check("graph", secrets)``'s result on success; raises
    :class:`ConnectorValidationError` (nothing stored) on a bad credential.
    """
    tenant_id = str(payload.get("tenant_id") or "").strip()
    client_id = str(payload.get("client_id") or "").strip()
    client_secret = str(payload.get("client_secret") or "")
    if not (tenant_id and client_id and client_secret):
        raise ConnectorValidationError("tenant_id, client_id, and client_secret are required")

    candidate = {"tenant_id": tenant_id, "client_id": client_id, "client_secret": client_secret}

    from backend.connectors.graph.connector import GraphConnector

    connector = GraphConnector(_StaticSecrets(candidate))
    try:
        connector._fetch_token()
    except Exception as exc:  # noqa: BLE001 - surfaced as a clear validation error
        raise ConnectorValidationError(f"Graph token request failed: {exc}") from exc

    secrets.put("aphrodite/graph", candidate)
    return run_check("graph", secrets)


def clio_authorize_url(payload: dict[str, Any]) -> dict[str, Any]:
    """Build the Clio out-of-band authorize link for an admin to send/open.

    No Secrets Manager write here — Clio shows the code on-screen rather
    than redirecting to us; the admin pastes it into :func:`clio_exchange`.
    """
    client_id = str(payload.get("client_id") or "").strip()
    if not client_id:
        raise ConnectorValidationError("client_id is required")

    from backend.connectors.clio.oauth_bootstrap import build_authorize_url

    kwargs: dict[str, Any] = {}
    base_url = str(payload.get("base_url") or "").strip()
    if base_url:
        kwargs["base_url"] = base_url
    url, state = build_authorize_url(client_id, **kwargs)
    return {"url": url, "state": state}


def clio_exchange(secrets: SecretsClient, payload: dict[str, Any]) -> dict[str, Any]:
    """Exchange a pasted Clio authorization code and store the refresh token.

    Returns ``run_check("clio", secrets)``'s result on success; raises
    :class:`ConnectorValidationError` (nothing stored) on a bad exchange.
    """
    client_id = str(payload.get("client_id") or "").strip()
    client_secret = str(payload.get("client_secret") or "")
    code = str(payload.get("code") or "").strip()
    if not (client_id and client_secret and code):
        raise ConnectorValidationError("client_id, client_secret, and code are required")

    from backend.connectors.clio.oauth_bootstrap import exchange_code
    from services.http import HttpError

    kwargs: dict[str, Any] = {}
    base_url = str(payload.get("base_url") or "").strip()
    if base_url:
        kwargs["base_url"] = base_url
    try:
        token_payload = exchange_code(client_id, client_secret, code, **kwargs)
    except HttpError as exc:
        raise ConnectorValidationError(f"Clio authorization-code exchange failed: {exc}") from exc

    candidate: dict[str, Any] = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": token_payload["refresh_token"],
    }
    if base_url:
        candidate["base_url"] = base_url

    secrets.put("aphrodite/clio", candidate)
    return run_check("clio", secrets)
