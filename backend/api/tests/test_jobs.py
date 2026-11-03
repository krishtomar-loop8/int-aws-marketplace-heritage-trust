"""Unit tests for the async job store (in-memory + DynamoDB shapes)."""

from __future__ import annotations

from typing import Any

from backend.api.jobs import (
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_PENDING,
    DynamoDbJobStore,
    InMemoryJobStore,
)


def _clock() -> float:
    return 1_700_000_000.0


def _incrementing() -> Any:
    """A clock that advances 1s per call, so create/complete get distinct times."""
    counter = {"t": 0.0}

    def clock() -> float:
        counter["t"] += 1.0
        return counter["t"]

    return clock


# -- in-memory --------------------------------------------------------------


def test_inmemory_create_get_complete() -> None:
    store = InMemoryJobStore(clock=_clock)
    store.create("j1", "status on Smith", {"sub": "user-1"})
    assert store.get("j1")["status"] == STATUS_PENDING

    store.complete("j1", {"answer": "ok"})
    item = store.get("j1")
    assert item["status"] == STATUS_DONE
    assert item["result"] == {"answer": "ok"}


def test_inmemory_delete() -> None:
    store = InMemoryJobStore(clock=_clock)
    store.create("j1", "status on Smith", {"sub": "user-1"})
    store.delete("j1")
    assert store.get("j1") is None
    store.delete("never-existed")  # no error


def test_inmemory_fail() -> None:
    store = InMemoryJobStore(clock=_clock)
    store.create("j2", "q", None)
    store.fail("j2", "boom")
    assert store.get("j2")["status"] == STATUS_ERROR
    assert store.get("j2")["error"] == "boom"


def test_inmemory_missing_job_is_none() -> None:
    assert InMemoryJobStore().get("nope") is None


def test_inmemory_progress_sets_phase_and_keeps_pending() -> None:
    store = InMemoryJobStore(clock=_clock)
    store.create("j3", "q", None)
    store.progress("j3", "Reading Estateably, Clio and Outlook")
    got = store.get("j3")
    assert got["phase"] == "Reading Estateably, Clio and Outlook"
    assert got["status"] == STATUS_PENDING  # progress must not flip status


# -- DynamoDB shapes (fake client) ------------------------------------------


class FakeDynamo:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []

    def put_item(self, *, TableName: str, Item: dict[str, Any]) -> None:
        self.calls.append("put")
        self.items[Item["job_id"]["S"]] = dict(Item)

    def get_item(self, *, TableName: str, Key: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("get")
        item = self.items.get(Key["job_id"]["S"])
        return {"Item": item} if item else {}

    def delete_item(self, *, TableName: str, Key: dict[str, Any]) -> None:
        self.calls.append("delete")
        self.items.pop(Key["job_id"]["S"], None)

    def update_item(self, *, Key: dict[str, Any], **kw: Any) -> None:
        self.calls.append("update")
        item = self.items[Key["job_id"]["S"]]
        # Generic SET applier: resolve each "lhs = :ph" pair via the name/value maps
        # so it works for every UpdateExpression shape (complete/fail/progress).
        names = kw.get("ExpressionAttributeNames") or {}
        values = kw["ExpressionAttributeValues"]
        assignments = kw["UpdateExpression"].replace("SET", "", 1)
        for pair in assignments.split(","):
            lhs, rhs = (p.strip() for p in pair.split("="))
            item[names.get(lhs, lhs)] = values[rhs]

    def query(
        self,
        *,
        TableName: str,
        IndexName: str,
        KeyConditionExpression: str,
        ExpressionAttributeValues: dict[str, Any],
        ScanIndexForward: bool = True,
        Limit: int = 50,
    ) -> dict[str, Any]:
        self.calls.append("query")
        sub = ExpressionAttributeValues[":s"]["S"]
        matched = [i for i in self.items.values() if i.get("actor_sub", {}).get("S") == sub]
        matched.sort(
            key=lambda i: int(i.get("created_at", {}).get("N", "0")),
            reverse=not ScanIndexForward,
        )
        return {"Items": matched[:Limit]}


def test_dynamo_create_sets_pending_and_ttl() -> None:
    fake = FakeDynamo()
    store = DynamoDbJobStore("jobs", client=fake, ttl_seconds=100, clock=_clock)
    store.create("j1", "q", {"sub": "u1"})
    stored = fake.items["j1"]
    assert stored["status"]["S"] == STATUS_PENDING
    assert stored["expires_at"]["N"] == str(int(_clock()) + 100)
    assert stored["actor_sub"]["S"] == "u1"


def test_dynamo_delete_removes_item() -> None:
    fake = FakeDynamo()
    store = DynamoDbJobStore("jobs", client=fake, clock=_clock)
    store.create("j1", "q", {"sub": "u1"})
    store.delete("j1")
    assert store.get("j1") is None
    assert "delete" in fake.calls


def test_dynamo_complete_roundtrips_result_as_json() -> None:
    fake = FakeDynamo()
    store = DynamoDbJobStore("jobs", client=fake, clock=_clock)
    store.create("j1", "q", None)
    store.complete("j1", {"answer": {"sections": {"a": "b"}}})
    got = store.get("j1")
    assert got["status"] == STATUS_DONE
    assert got["result"] == {"answer": {"sections": {"a": "b"}}}


def test_dynamo_fail_records_error() -> None:
    fake = FakeDynamo()
    store = DynamoDbJobStore("jobs", client=fake, clock=_clock)
    store.create("j1", "q", None)
    store.fail("j1", "kaboom")
    assert store.get("j1")["status"] == STATUS_ERROR
    assert store.get("j1")["error"] == "kaboom"


def test_dynamo_missing_is_none() -> None:
    store = DynamoDbJobStore("jobs", client=FakeDynamo(), clock=_clock)
    assert store.get("absent") is None


# -- per-user history (list_for_actor + matter_name) ------------------------


def test_inmemory_list_for_actor_is_isolated_and_newest_first() -> None:
    times = iter([100.0, 200.0, 300.0])
    store = InMemoryJobStore(clock=lambda: next(times))
    store.create("a1", "q1", {"sub": "alice"})
    store.create("b1", "q2", {"sub": "bob"})
    store.create("a2", "q3", {"sub": "alice"})
    rows = store.list_for_actor("alice")
    assert [r["job_id"] for r in rows] == ["a2", "a1"]  # newest first, bob excluded


def test_inmemory_complete_records_matter_name_for_history() -> None:
    store = InMemoryJobStore(clock=_clock)
    store.create("j1", "yurkiw", {"sub": "u1"})
    store.complete("j1", {"status": "ok", "matter": {"name": "Estate of Yurkiw"}})
    assert store.get("j1")["matter_name"] == "Estate of Yurkiw"
    assert store.list_for_actor("u1")[0]["matter_name"] == "Estate of Yurkiw"


def test_inmemory_latest_source_errors_from_most_recent_full_search() -> None:
    store = InMemoryJobStore(clock=_incrementing())
    store.create("old", "q", {"sub": "u"})
    store.complete("old", {"status": "ok", "dossier_errors": ["QuickBooks unavailable"]})
    store.create("new", "q", {"sub": "u"})
    store.complete("new", {"status": "ok", "dossier_errors": ["outlook: 1 failed"]})
    assert store.latest_source_errors("u") == ["outlook: 1 failed"]  # newest full search


def test_inmemory_latest_source_errors_skips_quick_and_pending() -> None:
    store = InMemoryJobStore(clock=_incrementing())
    store.create("full", "q", {"sub": "u"})
    store.complete("full", {"status": "ok", "dossier_errors": ["QuickBooks unavailable"]})
    store.create("quick", "q", {"sub": "u"})
    store.complete("quick", {"status": "answer"})  # quick answer → no dossier → skipped
    store.create("pending", "q", {"sub": "u"})  # still running → skipped
    assert store.latest_source_errors("u") == ["QuickBooks unavailable"]


def test_inmemory_latest_source_errors_empty_when_clean() -> None:
    store = InMemoryJobStore(clock=_incrementing())
    store.create("j", "q", {"sub": "u"})
    store.complete("j", {"status": "ok"})  # no dossier_errors
    assert store.latest_source_errors("u") == []


def test_dynamo_latest_source_errors_reads_most_recent_full_search() -> None:
    fake = FakeDynamo()
    store = DynamoDbJobStore("jobs", client=fake, clock=_incrementing())
    store.create("old", "q", {"sub": "u"})
    store.complete("old", {"status": "ok", "dossier_errors": ["QuickBooks unavailable"]})
    store.create("new", "q", {"sub": "u"})
    store.complete("new", {"status": "ok", "dossier_errors": ["outlook: 1 failed"]})
    assert store.latest_source_errors("u") == ["outlook: 1 failed"]


def test_dynamo_list_for_actor_queries_gsi_isolated_newest_first() -> None:
    fake = FakeDynamo()
    times = iter([100.0, 200.0, 300.0])
    store = DynamoDbJobStore("jobs", client=fake, clock=lambda: next(times))
    store.create("a1", "q1", {"sub": "alice"})
    store.create("b1", "q2", {"sub": "bob"})
    store.create("a2", "q3", {"sub": "alice"})
    rows = store.list_for_actor("alice")
    assert [r["job_id"] for r in rows] == ["a2", "a1"]  # bob's job never surfaces
    assert "query" in rows[0]  # list-view fields projected


def test_dynamo_complete_records_matter_name_and_get_exposes_owner() -> None:
    fake = FakeDynamo()
    store = DynamoDbJobStore("jobs", client=fake, clock=_clock)
    store.create("j1", "yurkiw", {"sub": "u1"})
    store.complete("j1", {"status": "ok", "matter": {"name": "Estate of Yurkiw"}})
    got = store.get("j1")
    assert got["matter_name"] == "Estate of Yurkiw"
    assert got["actor_sub"] == "u1"  # backs the per-user owner check
    assert got["query"] == "yurkiw"
