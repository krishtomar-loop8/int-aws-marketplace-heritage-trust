"""Async search-job store.

Fan-out + synthesis can exceed API Gateway's 29s integration timeout, so search
runs as an async job: ``POST /search`` creates a job and returns its id; a worker
processes it; ``GET /search/{id}`` polls. This module is the job store — an
in-memory implementation for tests/local, and a DynamoDB implementation for
Lambda.

Timestamps are injected (``clock``) so tests are deterministic and the module has
no hidden time dependency.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from typing import Any, Protocol

STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_ERROR = "error"

# 30-day retention for the per-user search history (the status file is client PII;
# encrypted at rest with the project CMK — see the jobs table terraform).
DEFAULT_TTL_SECONDS = 30 * 86_400
ACTOR_INDEX = "by-actor"


def _matter_name(result: dict[str, Any]) -> str | None:
    """The resolved matter name for a completed search, if any (None for
    not_found/disambiguation) — used as the history list label."""
    return (result.get("matter") or {}).get("name")


def _source_errors_of_latest(results: Iterable[dict[str, Any] | None]) -> list[str]:
    """Dossier errors from the most recent full ("ok") search — the connector-health
    signal for the in-app banner. Quick answers / not_found carry no dossier, so
    they're skipped (they never clear a standing source-unavailable state)."""
    for result in results:
        if result and result.get("status") == "ok":
            return list(result.get("dossier_errors") or [])
    return []


def _list_row(item: dict[str, Any]) -> dict[str, Any]:
    """Project a stored (native-dict) job to a history list-view row — no result
    blob (fetched on demand when a search is re-opened)."""
    return {
        "job_id": item.get("job_id"),
        "status": item.get("status"),
        "query": item.get("query"),
        "matter_name": item.get("matter_name"),
        "created_at": item.get("created_at"),
        "phase": item.get("phase"),
    }


class JobStore(Protocol):
    def create(self, job_id: str, query: str, actor: dict[str, Any] | None) -> dict[str, Any]: ...
    def get(self, job_id: str) -> dict[str, Any] | None: ...
    def complete(self, job_id: str, result: dict[str, Any]) -> None: ...
    def fail(self, job_id: str, error: str) -> None: ...
    def progress(self, job_id: str, phase: str) -> None: ...
    def delete(self, job_id: str) -> None: ...
    def list_for_actor(self, actor_sub: str, *, limit: int = 50) -> list[dict[str, Any]]: ...
    def latest_source_errors(self, actor_sub: str) -> list[str]: ...


class InMemoryJobStore:
    """Process-local store for tests and local runs."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._clock = clock

    def create(self, job_id: str, query: str, actor: dict[str, Any] | None) -> dict[str, Any]:
        item = {
            "job_id": job_id,
            "status": STATUS_PENDING,
            "query": query,
            "actor_sub": (actor or {}).get("sub"),
            "created_at": self._clock(),
        }
        self._items[job_id] = item
        return item

    def get(self, job_id: str) -> dict[str, Any] | None:
        return self._items.get(job_id)

    def complete(self, job_id: str, result: dict[str, Any]) -> None:
        item = self._items.get(job_id)
        if item is not None:
            item.update(
                status=STATUS_DONE,
                result=result,
                matter_name=_matter_name(result),
                updated_at=self._clock(),
            )

    def fail(self, job_id: str, error: str) -> None:
        item = self._items.get(job_id)
        if item is not None:
            item.update(status=STATUS_ERROR, error=error, updated_at=self._clock())

    def progress(self, job_id: str, phase: str) -> None:
        item = self._items.get(job_id)
        if item is not None:
            item["phase"] = phase

    def delete(self, job_id: str) -> None:
        self._items.pop(job_id, None)

    def list_for_actor(self, actor_sub: str, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = [i for i in self._items.values() if i.get("actor_sub") == actor_sub]
        rows.sort(key=lambda i: i.get("created_at") or 0, reverse=True)
        return [_list_row(i) for i in rows[:limit]]

    def latest_source_errors(self, actor_sub: str) -> list[str]:
        rows = [i for i in self._items.values() if i.get("actor_sub") == actor_sub]
        rows.sort(key=lambda i: i.get("created_at") or 0, reverse=True)
        return _source_errors_of_latest(
            i.get("result") for i in rows if i.get("status") == STATUS_DONE
        )


class DynamoDbJobStore:
    """DynamoDB-backed store for the Lambda deployment.

    The table has a ``job_id`` (string) partition key and a TTL attribute
    ``expires_at`` so old jobs self-expire. ``result`` is stored as JSON text to
    avoid DynamoDB's float/empty-value constraints.
    """

    def __init__(
        self,
        table_name: str,
        *,
        client: Any | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        index_name: str = ACTOR_INDEX,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._table_name = table_name
        self._client = client
        self._ttl = ttl_seconds
        self._index_name = index_name
        self._clock = clock

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("dynamodb")
        return self._client

    def create(self, job_id: str, query: str, actor: dict[str, Any] | None) -> dict[str, Any]:
        now = int(self._clock())
        self.client.put_item(
            TableName=self._table_name,
            Item={
                "job_id": {"S": job_id},
                "status": {"S": STATUS_PENDING},
                "query": {"S": query},
                # GSI partition key for the per-user history list.
                "actor_sub": {"S": (actor or {}).get("sub") or "-"},
                "created_at": {"N": str(now)},
                "expires_at": {"N": str(now + self._ttl)},
            },
        )
        return {"job_id": job_id, "status": STATUS_PENDING}

    def get(self, job_id: str) -> dict[str, Any] | None:
        import json

        resp = self.client.get_item(TableName=self._table_name, Key={"job_id": {"S": job_id}})
        item = resp.get("Item")
        if not item:
            return None
        out: dict[str, Any] = {
            "job_id": item["job_id"]["S"],
            "status": item["status"]["S"],
        }
        # actor_sub backs the per-user owner check; fall back to the legacy "actor"
        # attribute for rows written before the rename.
        if "actor_sub" in item:
            out["actor_sub"] = item["actor_sub"]["S"]
        elif "actor" in item:
            out["actor_sub"] = item["actor"]["S"]
        for key in ("query", "matter_name"):
            if key in item:
                out[key] = item[key]["S"]
        if "created_at" in item:
            out["created_at"] = float(item["created_at"]["N"])
        if "result" in item:
            out["result"] = json.loads(item["result"]["S"])
        if "error" in item:
            out["error"] = item["error"]["S"]
        if "phase" in item:
            out["phase"] = item["phase"]["S"]
        return out

    def complete(self, job_id: str, result: dict[str, Any]) -> None:
        import json

        values: dict[str, Any] = {
            ":s": {"S": STATUS_DONE},
            ":r": {"S": json.dumps(result, default=str)},
            ":u": {"N": str(int(self._clock()))},
        }
        expr = "SET #s = :s, #r = :r, updated_at = :u"
        names = {"#s": "status", "#r": "result"}
        matter_name = _matter_name(result)
        if matter_name:
            expr += ", matter_name = :m"
            values[":m"] = {"S": matter_name[:512]}
        self.client.update_item(
            TableName=self._table_name,
            Key={"job_id": {"S": job_id}},
            UpdateExpression=expr,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    def fail(self, job_id: str, error: str) -> None:
        self._update(job_id, STATUS_ERROR, "error", {"S": error[:1024]})

    def delete(self, job_id: str) -> None:
        self.client.delete_item(TableName=self._table_name, Key={"job_id": {"S": job_id}})

    def progress(self, job_id: str, phase: str) -> None:
        # Sets phase without touching status (job stays pending); the poll reads it.
        self.client.update_item(
            TableName=self._table_name,
            Key={"job_id": {"S": job_id}},
            UpdateExpression="SET phase = :p, updated_at = :u",
            ExpressionAttributeValues={
                ":p": {"S": phase[:200]},
                ":u": {"N": str(int(self._clock()))},
            },
        )

    def list_for_actor(self, actor_sub: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """The caller's recent jobs, newest-first, via the by-actor GSI. Returns
        list-view rows only (no result blob — the GSI doesn't project it)."""
        resp = self.client.query(
            TableName=self._table_name,
            IndexName=self._index_name,
            KeyConditionExpression="actor_sub = :s",
            ExpressionAttributeValues={":s": {"S": actor_sub}},
            ScanIndexForward=False,  # newest first
            Limit=limit,
        )
        rows: list[dict[str, Any]] = []
        for item in resp.get("Items", []):
            row: dict[str, Any] = {
                "job_id": item["job_id"]["S"],
                "status": item.get("status", {}).get("S", ""),
            }
            for key in ("query", "matter_name", "phase"):
                if key in item:
                    row[key] = item[key]["S"]
            if "created_at" in item:
                row["created_at"] = float(item["created_at"]["N"])
            rows.append(row)
        return rows

    def latest_source_errors(self, actor_sub: str, *, scan_limit: int = 10) -> list[str]:
        """Source errors from the caller's most recent full search (connector-health
        banner). The GSI projects ``status``, so we page the newest jobs, then GET
        only the completed ones (lazily) until the first full ``ok`` result."""
        resp = self.client.query(
            TableName=self._table_name,
            IndexName=self._index_name,
            KeyConditionExpression="actor_sub = :s",
            ExpressionAttributeValues={":s": {"S": actor_sub}},
            ScanIndexForward=False,  # newest first
            Limit=scan_limit,
        )
        done_ids = [
            it["job_id"]["S"]
            for it in resp.get("Items", [])
            if it.get("status", {}).get("S") == STATUS_DONE
        ]
        return _source_errors_of_latest((self.get(jid) or {}).get("result") for jid in done_ids)

    def _update(self, job_id: str, status: str, field: str, value: dict[str, Any]) -> None:
        self.client.update_item(
            TableName=self._table_name,
            Key={"job_id": {"S": job_id}},
            UpdateExpression="SET #s = :s, #f = :v, updated_at = :u",
            ExpressionAttributeNames={"#s": "status", "#f": field},
            ExpressionAttributeValues={
                ":s": {"S": status},
                ":v": value,
                ":u": {"N": str(int(self._clock()))},
            },
        )
