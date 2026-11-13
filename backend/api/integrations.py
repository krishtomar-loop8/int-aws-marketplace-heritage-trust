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

    Thin wrapper over the shared catalog (``services.errors.classify``) —
    kept as its own function because its two existing callers (the health
    probe and connector-connect admin path) only need the message string.
    """
    from services.errors import classify

    return classify(detail, integration=integration).message


@dataclass
class IntegrationHealthStore:
    """DynamoDB-backed integration health: current status + append-only downtime log."""

    table: str
    client: Any | None = None  # boto3 dynamodb client; lazily built in the Lambda
    clock: Callable[[], float] = time.time
    # Called once per outage, when consecutive_failures first reaches
    # ALERT_THRESHOLD — not on every failure (a single blip is usually
    # transient) and not re-sent on every subsequent failure of the same
    # outage. None (the default) means no alerting is configured.
    on_alert: Callable[[str, str], None] | None = None
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

        if not ok and consecutive == ALERT_THRESHOLD and self.on_alert:
            self.on_alert(integration, detail or "ok")

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

    def snapshot(
        self, *, log_limit: int = 50, enabled: frozenset[str] | None = None
    ) -> dict[str, Any]:
        """Return current per-integration status + the recent downtime log.

        ``enabled``, when given, drops any row for a connector this deployment
        has since disabled — a stale "down" row written before the pause must
        not keep alerting once nothing is probing that connector any more.
        """
        integrations = self._all_current()
        if enabled is not None:
            integrations = [row for row in integrations if row["integration"] in enabled]
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
