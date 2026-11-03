"""Chain-of-custody activity log.

A durable, append-only record of *who did what, when, and which source systems
were read* — the audit trail the portal's Activity Logs tab surfaces in plain
language. Every staff action (matter searches, report generation, AML runs and
schedule changes, sanction screening, certificate issue/removal, downloads) is
recorded as one row in a DynamoDB table.

The table has a constant ``log_id`` partition key ("activity") and a sortable
``ts`` range key ("<ISO8601 UTC>#<uuid>"), so the whole log lives in one partition
and reads newest-first in a single ``Query`` — ample for the POC's volume. The
Lambda is granted ``PutItem`` + ``Query`` only (no delete/update), so records are
append-only and can't be altered through the app.

Writing an audit row must never break the action it describes: :meth:`record`
swallows and logs any failure rather than raising into the request path — the
same discipline the search worker uses for progress writes.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from services.logging import get_logger

logger = get_logger(__name__)

_PARTITION = "activity"
_DEFAULT_LIMIT = 500


@dataclass
class ActivityLog:
    """DynamoDB-backed append-only activity/audit trail."""

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

    def record(
        self,
        actor: dict[str, Any] | None,
        action: str,
        summary: str,
        *,
        target: str | None = None,
        sources: list[str] | None = None,
        detail: dict[str, Any] | None = None,
        status: str = "success",
    ) -> None:
        """Append one activity row. Never raises — a failed audit write must not
        break the user action it records (logs a warning instead)."""
        now = datetime.fromtimestamp(self.clock(), tz=UTC).isoformat()
        event_id = uuid.uuid4().hex
        item: dict[str, Any] = {
            "log_id": {"S": _PARTITION},
            "ts": {"S": f"{now}#{event_id}"},
            "id": {"S": event_id},
            "timestamp": {"S": now},
            "actor_email": {"S": (actor or {}).get("email") or "unknown"},
            "actor_sub": {"S": (actor or {}).get("sub") or "-"},
            "action": {"S": action},
            "summary": {"S": summary[:1024]},
            "status": {"S": status},
        }
        if target:
            item["target"] = {"S": str(target)[:512]}
        if sources:
            item["sources"] = {"L": [{"S": str(s)} for s in sources]}
        if detail:
            item["detail"] = {"S": json.dumps(detail, default=str)[:2048]}
        try:
            self.ddb.put_item(TableName=self.table, Item=item)
        except Exception:  # noqa: BLE001 - auditing must never break the action
            logger.warning(
                "activity write failed",
                extra={"extra_fields": {"action": action, "actor": (actor or {}).get("sub")}},
            )

    def list(self, limit: int = _DEFAULT_LIMIT) -> list[dict[str, Any]]:
        """Return the most recent activity rows, newest first."""
        limit = max(1, min(limit, 1000))
        resp = self.ddb.query(
            TableName=self.table,
            KeyConditionExpression="log_id = :p",
            ExpressionAttributeValues={":p": {"S": _PARTITION}},
            ScanIndexForward=False,  # newest first
            Limit=limit,
        )
        return [self._deserialize(item) for item in resp.get("Items", [])]

    @staticmethod
    def _deserialize(item: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": item.get("id", {}).get("S"),
            "timestamp": item.get("timestamp", {}).get("S"),
            "actor_email": item.get("actor_email", {}).get("S"),
            "actor_sub": item.get("actor_sub", {}).get("S"),
            "action": item.get("action", {}).get("S"),
            "summary": item.get("summary", {}).get("S"),
            "status": item.get("status", {}).get("S"),
        }
        if "target" in item:
            out["target"] = item["target"]["S"]
        if "sources" in item:
            out["sources"] = [s.get("S") for s in item["sources"].get("L", [])]
        if "detail" in item:
            try:
                out["detail"] = json.loads(item["detail"]["S"])
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
        return out
