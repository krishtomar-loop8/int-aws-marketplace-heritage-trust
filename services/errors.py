"""Shared error classification and staff-facing message catalog.

``classify`` maps a connector error detail (the harness's ``"<ExcType>: <message>"``
string) to an :class:`ErrorClass` and a plain-language message. Pattern-match order
and markers match ``backend.api.integrations.suggest_fix``.

When ``integration`` is given, messages name the connector (and the QuickBooks
re-authorize special case). When it is ``None``, messages are connector-agnostic.
"""

from __future__ import annotations

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


def classify(detail: str, *, integration: str | None = None) -> ClassifiedError:
    d = (detail or "").lower()
    named = integration is not None
    provider = "the provider" if named else "the source system"

    auth_markers = ("token refresh failed", "invalid_grant", "unauthorized", " 401", ":401")
    if any(k in d for k in auth_markers):
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
    if "402" in d:
        return ClassifiedError(
            ErrorClass.PERMANENT,
            f"{provider.capitalize()} reported a billing/payment hold on the account "
            "(HTTP 402). Check the provider's subscription/billing status."
            if named
            else "The source system reported a billing/payment hold on the account "
            "(HTTP 402). Check the source system's subscription/billing status.",
        )
    if "403" in d or "forbidden" in d:
        return ClassifiedError(
            ErrorClass.PERMANENT,
            "Access was denied (HTTP 403). Check the connector's API scopes/permissions "
            "and that the app is still authorized on the provider side."
            if named
            else "Access was denied (HTTP 403). Check the API scopes/permissions "
            "and that the app is still authorized on the source system side.",
        )
    if any(k in d for k in ("429", "throttl", "rate limit", "too many requests")):
        return ClassifiedError(
            ErrorClass.TRANSIENT,
            f"{provider.capitalize()} is rate-limiting requests (HTTP 429). This is usually "
            "transient and should clear on the next check.",
        )
    if any(k in d for k in ("timed out", "timeout", "readtimeout", "connecttimeout", "connection")):
        return ClassifiedError(
            ErrorClass.TRANSIENT,
            f"{provider.capitalize()} was slow or unreachable. This is usually transient — it "
            "should clear on the next check; if it persists, check the "
            f"{'provider' if named else 'source system'}'s status page.",
        )
    if any(k in d for k in ("500", "502", "503", "504", "serviceunavailable", "server error")):
        return ClassifiedError(
            ErrorClass.TRANSIENT,
            f"{provider.capitalize()} returned a server error (5xx) — likely a provider-side "
            "outage. It should recover on its own; retry shortly."
            if named
            else "The source system returned a server error (5xx) — likely a source-side "
            "outage. It should recover on its own; retry shortly.",
        )
    return ClassifiedError(
        ErrorClass.UNKNOWN,
        "The connector returned an error (see the detail). If it persists, check the "
        "credentials in Secrets Manager and the provider's status page."
        if named
        else "An error was returned (see the detail). If it persists, check the "
        "credentials in Secrets Manager and the source system's status page.",
    )
