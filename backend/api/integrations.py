"""Integration health monitor.

A scheduled probe (the search Lambda's ``health_probe`` path) runs the D2
connectivity harness against every source connector on a fixed cadence and
records the result here, so the portal's Integrations tab can show live status,
a downtime log, and a suggested fix when something is down — instead of a dead
connector failing silently mid-search (the expired-QuickBooks-token incident).

Storage (DynamoDB, one table ``aphrodite-integration-health``):

- a constant partition ``pk = "health"`` so a single Query returns everything;
- a mutable **current** row per integration (``sk = "cur#<name>"``) carrying the
  latest ok/detail/suggested-fix plus a ``consecutive_failures`` counter — the
  portal shows a pulsing alert once an integration fails 2 checks in a row;
- append-only **transition** rows (``sk = "log#<ISO8601>#<uuid>"``) written only
  when an integration flips up↔down — the downtime log.

:meth:`record_result` never raises — a failed health write must not break the
probe (same discipline as the activity log).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from services.logging import get_logger

logger = get_logger(__name__)

_PARTITION = "health"
_CUR_PREFIX = "cur#"
_LOG_PREFIX = "log#"
# Consecutive failed checks before the UI raises a (pulsing) alert. One blip is
# usually transient provider throttling; two in a row is a real outage.
ALERT_THRESHOLD = 2


def suggest_fix(integration: str, detail: str) -> str:
    """Map a connector error detail to a plain-language suggested fix for staff.

    ``detail`` is the harness's ``"<ExcType>: <message>"`` string. The mapping is
    deterministic (pattern match) and errs toward "likely transient" for network
    blips so staff aren't sent chasing a fix that resolves itself.
    """
    d = (detail or "").lower()
    auth_markers = ("token refresh failed", "invalid_grant", "unauthorized", " 401", ":401")
    if any(k in d for k in auth_markers):
        if integration == "quickbooks":
            return (
                "The QuickBooks OAuth token has expired or been revoked. Re-authorize the "
                "connection — run the QuickBooks bootstrap to mint a fresh refresh token "
                "(it self-rotates thereafter). See the QuickBooks setup guide."
            )
        return (
            "Authentication failed — the connector's token/credentials are expired or "
            "invalid. Re-authorize the connector or rotate its secret in Secrets Manager."
        )
    if "402" in d:
        return (
            "The provider reported a billing/payment hold on the account (HTTP 402). "
            "Check the provider's subscription/billing status."
        )
    if "403" in d or "forbidden" in d:
        return (
            "Access was denied (HTTP 403). Check the connector's API scopes/permissions "
            "and that the app is still authorized on the provider side."
        )
    if any(k in d for k in ("429", "throttl", "rate limit", "too many requests")):
        return (
            "The provider is rate-limiting requests (HTTP 429). This is usually transient "
            "and should clear on the next check."
        )
    if any(k in d for k in ("timed out", "timeout", "readtimeout", "connecttimeout", "connection")):
        return (
            "The provider was slow or unreachable. This is usually transient — it should "
            "clear on the next check; if it persists, check the provider's status page."
        )
    if any(k in d for k in ("500", "502", "503", "504", "serviceunavailable", "server error")):
        return (
            "The provider returned a server error (5xx) — likely a provider-side outage. "
            "It should recover on its own; retry shortly."
        )
    return (
        "The connector returned an error (see the detail). If it persists, check the "
        "credentials in Secrets Manager and the provider's status page."
    )


@dataclass
class IntegrationHealthStore:
    """DynamoDB-backed integration health: current status + append-only downtime log."""

    table: str
    client: Any | None = None  # boto3 dynamodb client; lazily built in the Lambda
    clock: Callable[[], float] = time.time
    _client: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._client = self.client

    @property
    def ddb(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("dynamodb")
        return self._client

    # -- write (scheduled probe) -------------------------------------------

    def record_result(
        self,
        integration: str,
        *,
        ok: bool,
        detail: str = "",
        suggested_fix: str = "",
    ) -> None:
        """Record one probe result. Never raises — a failed health write must not
        break the probe (logs a warning instead).

        Increments ``consecutive_failures`` on failure (reset to 0 on success) and,
        when the up/down state flips, appends a transition row to the downtime log.
        """
        now = datetime.fromtimestamp(self.clock(), tz=UTC).isoformat()
        prior = self._current(integration)
        prior_ok = prior.get("ok") if prior else None
        prior_failures = int(prior.get("consecutive_failures", 0)) if prior else 0
        consecutive = 0 if ok else prior_failures + 1

        item: dict[str, Any] = {
            "pk": {"S": _PARTITION},
            "sk": {"S": f"{_CUR_PREFIX}{integration}"},
            "integration": {"S": integration},
            "ok": {"BOOL": bool(ok)},
            "checked_at": {"S": now},
            "consecutive_failures": {"N": str(consecutive)},
            "detail": {"S": (detail or "ok")[:1024]},
        }
        if suggested_fix:
            item["suggested_fix"] = {"S": suggested_fix[:1024]}
        try:
            self.ddb.put_item(TableName=self.table, Item=item)
        except Exception:  # noqa: BLE001 - a failed health write must not break the probe
            logger.warning(
                "integration health write failed",
                extra={"extra_fields": {"integration": integration}},
            )
            return

        # Downtime log: append a row only when the state actually flips (both
        # directions), so the log is a clean history of outages and recoveries.
        if prior_ok is not None and bool(prior_ok) != bool(ok):
            self._log_transition(integration, ok=bool(ok), detail=detail, at=now)

    def _log_transition(self, integration: str, *, ok: bool, detail: str, at: str) -> None:
        event_id = uuid.uuid4().hex
        item = {
            "pk": {"S": _PARTITION},
            "sk": {"S": f"{_LOG_PREFIX}{at}#{event_id}"},
            "integration": {"S": integration},
            "ok": {"BOOL": ok},
            "at": {"S": at},
            "detail": {"S": (detail or "ok")[:1024]},
        }
        try:
            self.ddb.put_item(TableName=self.table, Item=item)
        except Exception:  # noqa: BLE001 - see record_result
            logger.warning(
                "integration health transition write failed",
                extra={"extra_fields": {"integration": integration}},
            )

    def _current(self, integration: str) -> dict[str, Any] | None:
        try:
            resp = self.ddb.get_item(
                TableName=self.table,
                Key={"pk": {"S": _PARTITION}, "sk": {"S": f"{_CUR_PREFIX}{integration}"}},
            )
        except Exception:  # noqa: BLE001 - treat a read miss as "no prior state"
            return None
        item = resp.get("Item")
        return self._deserialize_current(item) if item else None

    # -- read (GET /integrations) ------------------------------------------

    def snapshot(self, *, log_limit: int = 50) -> dict[str, Any]:
        """Return current per-integration status + the recent downtime log."""
        integrations = self._all_current()
        log = self._recent_transitions(log_limit)
        alerting = any(
            not row["ok"] and row["consecutive_failures"] >= ALERT_THRESHOLD for row in integrations
        )
        return {"integrations": integrations, "log": log, "alerting": alerting}

    def _all_current(self) -> list[dict[str, Any]]:
        resp = self.ddb.query(
            TableName=self.table,
            KeyConditionExpression="pk = :p AND begins_with(sk, :c)",
            ExpressionAttributeValues={":p": {"S": _PARTITION}, ":c": {"S": _CUR_PREFIX}},
        )
        rows = [self._deserialize_current(item) for item in resp.get("Items", [])]
        rows.sort(key=lambda r: r["integration"])
        return rows

    def _recent_transitions(self, limit: int) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 200))
        resp = self.ddb.query(
            TableName=self.table,
            KeyConditionExpression="pk = :p AND begins_with(sk, :l)",
            ExpressionAttributeValues={":p": {"S": _PARTITION}, ":l": {"S": _LOG_PREFIX}},
            ScanIndexForward=False,  # newest first
            Limit=limit,
        )
        return [self._deserialize_log(item) for item in resp.get("Items", [])]

    @staticmethod
    def _deserialize_current(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "integration": item.get("integration", {}).get("S"),
            "ok": item.get("ok", {}).get("BOOL", False),
            "checked_at": item.get("checked_at", {}).get("S"),
            "consecutive_failures": int(item.get("consecutive_failures", {}).get("N", "0")),
            "detail": item.get("detail", {}).get("S", ""),
            "suggested_fix": item.get("suggested_fix", {}).get("S", ""),
        }

    @staticmethod
    def _deserialize_log(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "integration": item.get("integration", {}).get("S"),
            "ok": item.get("ok", {}).get("BOOL", False),
            "at": item.get("at", {}).get("S"),
            "detail": item.get("detail", {}).get("S", ""),
        }
