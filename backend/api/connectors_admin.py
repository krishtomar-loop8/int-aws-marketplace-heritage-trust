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

import secrets as _pysecrets
from typing import TYPE_CHECKING, Any

from backend.harness import run_check
from services.logging import get_logger
from services.secrets import SecretsClient

if TYPE_CHECKING:
    from backend.api.integrations import IntegrationHealthStore

logger = get_logger(__name__)

_QUICKBOOKS_ENVIRONMENTS = ("sandbox", "production")


class ConnectorValidationError(Exception):
    """A submitted credential failed live validation; nothing was stored."""


def _record_health(
    integrations: "IntegrationHealthStore | None", name: str, result: dict[str, Any]
) -> None:
    """Write a just-completed connect/reconnect's result into the health
    store immediately, so the Setup screen's status updates without waiting
    up to an hour for the next scheduled probe. Never raises — a health-store
    write must not turn a real success into an error response."""
    if integrations is None:
        return
    try:
        from backend.api.integrations import suggest_fix

        ok = bool(result.get("ok"))
        detail = str(result.get("detail", ""))
        fix = "" if ok else suggest_fix(name, detail)
        integrations.record_result(name, ok=ok, detail=detail, suggested_fix=fix)
    except Exception:  # noqa: BLE001 - see docstring
        logger.warning("health-store write after connect failed", extra={"extra_fields": {"integration": name}})


class _StaticSecrets:
    """A SecretsClient-shaped object serving one fixed, in-memory secret.

    Backs a throwaway connector instance used only to prove a submitted
    credential works before it is ever written to Secrets Manager.
    """

    def __init__(self, value: dict[str, Any]) -> None:
        self._value = value

    def get(self, name: str, *, refresh: bool = False) -> dict[str, Any]:
        return self._value


def connect_estateably(
    secrets: SecretsClient,
    payload: dict[str, Any],
    *,
    integrations: "IntegrationHealthStore | None" = None,
) -> dict[str, Any]:
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
    result = run_check("estateably", secrets)
    _record_health(integrations, "estateably", result)
    return result


def connect_graph(
    secrets: SecretsClient,
    payload: dict[str, Any],
    *,
    integrations: "IntegrationHealthStore | None" = None,
) -> dict[str, Any]:
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
    result = run_check("graph", secrets)
    _record_health(integrations, "graph", result)
    return result


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


def clio_exchange(
    secrets: SecretsClient,
    payload: dict[str, Any],
    *,
    integrations: "IntegrationHealthStore | None" = None,
) -> dict[str, Any]:
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
    result = run_check("clio", secrets)
    _record_health(integrations, "clio", result)
    return result


def quickbooks_start(
    secrets: SecretsClient, payload: dict[str, Any], *, redirect_uri: str
) -> dict[str, Any]:
    """Stage pending QuickBooks app credentials and return the authorize URL.

    The pending credentials are written under a ``_pending`` sub-key of the
    *existing* secret (not over it) — holding no refresh token yet, so the
    callback (which Intuit invokes with no way to ask the admin for
    client_id/client_secret again) can read them back out. Any already-working
    credential (``client_id``/``refresh_token``/``realm_id`` from a prior
    connect) is left untouched until the callback actually succeeds, so a
    reconnect that's never completed (the admin closes the tab, a wrong
    client_secret) can't destroy a working connection.
    """
    client_id = str(payload.get("client_id") or "").strip()
    client_secret = str(payload.get("client_secret") or "")
    environment = str(payload.get("environment") or "sandbox").strip().lower()
    if not (client_id and client_secret):
        raise ConnectorValidationError("client_id and client_secret are required")
    if environment not in _QUICKBOOKS_ENVIRONMENTS:
        raise ConnectorValidationError(
            f"environment must be one of {_QUICKBOOKS_ENVIRONMENTS!r}"
        )

    try:
        current = secrets.get("aphrodite/quickbooks")
    except Exception:  # noqa: BLE001 - no existing secret yet is the common first-connect case
        current = {}
    current = dict(current) if isinstance(current, dict) else {}

    state = _pysecrets.token_urlsafe(24)
    current["_pending"] = {
        "client_id": client_id,
        "client_secret": client_secret,
        "environment": environment,
        "state": state,
    }
    secrets.put("aphrodite/quickbooks", current)

    from backend.connectors.quickbooks.oauth_bootstrap import build_authorize_url

    url, _state = build_authorize_url(client_id, redirect_uri=redirect_uri, state=state)
    return {"url": url}


def _clear_pending(secrets: SecretsClient) -> None:
    """Best-effort: drop a stale ``_pending`` entry so a replayed/expired
    callback link can't be completed later. Never raises."""
    try:
        stored = secrets.get("aphrodite/quickbooks", refresh=True)
        if isinstance(stored, dict) and "_pending" in stored:
            stored = dict(stored)
            stored.pop("_pending", None)
            secrets.put("aphrodite/quickbooks", stored)
    except Exception:  # noqa: BLE001 - best-effort cleanup only
        pass


def quickbooks_callback(
    secrets: SecretsClient,
    query: dict[str, Any],
    *,
    redirect_uri: str,
    app_base_url: str,
    integrations: "IntegrationHealthStore | None" = None,
) -> str:
    """Complete the QuickBooks OAuth redirect; always returns a redirect URL.

    Never raises — its only caller is a browser-facing GET with no way to
    render a JSON error, so any failure (missing params, a state mismatch, a
    failed exchange, a missing realmId) redirects to an error page instead,
    and leaves any existing working credential exactly as it was.
    """
    try:
        stored = secrets.get("aphrodite/quickbooks", refresh=True)
        pending = stored.get("_pending") if isinstance(stored, dict) else None
        if not isinstance(pending, dict):
            raise ConnectorValidationError("no pending QuickBooks connection")

        code = query.get("code")
        state = query.get("state")
        realm_id = query.get("realmId")
        if not code or not state or not realm_id or state != pending.get("state"):
            raise ConnectorValidationError("missing or mismatched OAuth callback parameters")

        from backend.connectors.quickbooks.oauth_bootstrap import exchange_code

        token_payload = exchange_code(
            pending["client_id"], pending["client_secret"], code, redirect_uri=redirect_uri
        )

        stored = dict(stored)
        stored.pop("_pending", None)
        stored["client_id"] = pending["client_id"]
        stored["client_secret"] = pending["client_secret"]
        stored["environment"] = pending["environment"]
        stored["refresh_token"] = token_payload["refresh_token"]
        stored["realm_id"] = realm_id
        secrets.put("aphrodite/quickbooks", stored)

        try:
            result = run_check("quickbooks", secrets)
        except Exception:  # noqa: BLE001 - a health-check hiccup must not undo a real success
            result = {"ok": True, "detail": "ok"}
        _record_health(integrations, "quickbooks", result)

        return f"{app_base_url}/?setup=quickbooks-connected"
    except Exception as exc:  # noqa: BLE001 - this function must never raise; see docstring
        logger.warning(
            "QuickBooks callback failed", extra={"extra_fields": {"error_type": type(exc).__name__}}
        )
        _clear_pending(secrets)
        return f"{app_base_url}/?setup=quickbooks-error"
