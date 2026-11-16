"""Shared error classification and staff-facing message catalog.

``classify`` maps an error detail (a connector's, or any other classifiable
exception's — the harness's ``"<ExcType>: <message>"`` string) to an
:class:`ErrorClass` and a plain-language message. Pattern-match order and
markers match ``backend.api.integrations.suggest_fix``.

When ``integration`` is given, messages name the connector (and the QuickBooks
re-authorize special case). When it is ``None``, messages are connector-agnostic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class ErrorClass(StrEnum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    NEEDS_REAUTH = "needs_reauth"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ClassifiedError:
    error_class: ErrorClass
    message: str


_STATUS_PREFIX = r"(?:^|[\s:(])"


def _has_status_code(d: str, *codes: str) -> bool:
    """True if any of ``codes`` appears as a status code, not embedded in a URL/id.

    Requires the digits be preceded by start-of-string, whitespace, a colon, or
    an open-paren (not any non-word character — e.g. not ``/`` or ``.``), and
    followed by a word boundary.
    """
    return re.search(_STATUS_PREFIX + r"(?:" + "|".join(codes) + r")\b", d) is not None


def classify(detail: str, *, integration: str | None = None) -> ClassifiedError:
    d = (detail or "").lower()
    named = integration is not None
    provider = "the provider" if named else "the service"

    auth_markers = ("token refresh failed", "invalid_grant", "unauthorized")
    if any(k in d for k in auth_markers) or _has_status_code(d, "401"):
        if integration == "quickbooks":
            message = (
                "The QuickBooks OAuth token has expired or been revoked. Re-authorize the "
                "connection — run the QuickBooks bootstrap to mint a fresh refresh token "
                "(it self-rotates thereafter). See the QuickBooks setup guide."
            )
        elif named:
            message = (
                "Authentication failed — the connector's token/credentials are expired or "
                "invalid. Re-authorize the connector or rotate its secret in Secrets Manager."
            )
        else:
            message = (
                "Authentication failed — the token/credentials are expired or invalid. "
                "Re-authorize the account or rotate its secret in Secrets Manager."
            )
        return ClassifiedError(ErrorClass.NEEDS_REAUTH, message)
    if _has_status_code(d, "402"):
        return ClassifiedError(
            ErrorClass.PERMANENT,
            f"{provider.capitalize()} reported a billing/payment hold on the account "
            "(HTTP 402). Check the provider's subscription/billing status."
            if named
            else "A billing/payment hold was reported (HTTP 402). Check the account's "
            "subscription/billing status.",
        )
    if _has_status_code(d, "403") or "forbidden" in d:
        return ClassifiedError(
            ErrorClass.PERMANENT,
            "Access was denied (HTTP 403). Check the connector's API scopes/permissions "
            "and that the app is still authorized on the provider side."
            if named
            else "Access was denied (HTTP 403). Check the API scopes/permissions "
            "and that the app is still authorized on the service side.",
        )
    rate_limit_markers = ("throttl", "rate limit", "too many requests")
    if _has_status_code(d, "429") or any(k in d for k in rate_limit_markers):
        return ClassifiedError(
            ErrorClass.TRANSIENT,
            f"{provider.capitalize()} is rate-limiting requests (HTTP 429). This is usually "
            "transient and should clear on the next check."
            if named
            else "The request was rate-limited (HTTP 429). This is usually transient — "
            "please try again shortly.",
        )
    if any(k in d for k in ("timed out", "timeout", "readtimeout", "connecttimeout", "connection")):
        return ClassifiedError(
            ErrorClass.TRANSIENT,
            f"{provider.capitalize()} was slow or unreachable. This is usually transient — it "
            "should clear on the next check; if it persists, check the provider's status page."
            if named
            else "The request was slow or didn't complete. This is usually transient — "
            "please try again; if it persists, check the service's status.",
        )
    if (
        _has_status_code(d, "500", "502", "503", "504")
        or any(k in d for k in ("serviceunavailable", "server error"))
    ):
        return ClassifiedError(
            ErrorClass.TRANSIENT,
            f"{provider.capitalize()} returned a server error (5xx) — likely a provider-side "
            "outage. It should recover on its own; retry shortly."
            if named
            else "The service returned a server error (5xx) — likely an outage. "
            "It should recover on its own; retry shortly.",
        )
    return ClassifiedError(
        ErrorClass.UNKNOWN,
        "The connector returned an error (see the detail). If it persists, check the "
        "credentials in Secrets Manager and the provider's status page."
        if named
        else "An unexpected error occurred. If it persists, check the service's "
        "status and the request's configuration.",
    )
