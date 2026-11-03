"""Unit tests for the chain-of-custody activity log. No AWS — a fake DynamoDB."""

from __future__ import annotations

import json
from typing import Any

from backend.api.activity import ActivityLog


class FakeDynamo:
    def __init__(self, items: list[dict[str, Any]] | None = None, *, fail: bool = False) -> None:
        self.items = items or []
        self.put_calls: list[dict[str, Any]] = []
        self.query_calls: list[dict[str, Any]] = []
        self.fail = fail

    def put_item(self, **kw: Any) -> None:
        if self.fail:
            raise RuntimeError("boom")
        self.put_calls.append(kw)

    def query(self, **kw: Any) -> dict[str, Any]:
        self.query_calls.append(kw)
        return {"Items": self.items}


def test_record_writes_expected_item() -> None:
    ddb = FakeDynamo()
    log = ActivityLog("t", client=ddb, clock=lambda: 1_000_000.0)
    log.record(
        {"email": "a@b.ca", "sub": "s1"},
        "matter.search",
        "Ran a full matter search on “Smith Estate”",
        target="Smith Estate",
        sources=["clio", "estateably"],
        detail={"mode": "full"},
    )
    assert len(ddb.put_calls) == 1
    item = ddb.put_calls[0]["Item"]
    assert item["log_id"]["S"] == "activity"
    assert item["action"]["S"] == "matter.search"
    assert item["actor_email"]["S"] == "a@b.ca"
    assert item["actor_sub"]["S"] == "s1"
    assert item["target"]["S"] == "Smith Estate"
    assert [s["S"] for s in item["sources"]["L"]] == ["clio", "estateably"]
    assert json.loads(item["detail"]["S"]) == {"mode": "full"}
    # The sort key embeds the display timestamp, then a unique suffix.
    assert item["ts"]["S"].startswith(item["timestamp"]["S"] + "#")


def test_record_defaults_missing_actor_fields() -> None:
    ddb = FakeDynamo()
    ActivityLog("t", client=ddb).record(None, "download", "Downloaded a file")
    item = ddb.put_calls[0]["Item"]
    assert item["actor_email"]["S"] == "unknown"
    assert item["actor_sub"]["S"] == "-"
    assert item["status"]["S"] == "success"
    assert "sources" not in item and "target" not in item and "detail" not in item


def test_record_swallows_write_errors() -> None:
    ddb = FakeDynamo(fail=True)
    # Must not raise — a failed audit write can never break the user's action.
    ActivityLog("t", client=ddb).record({"sub": "s"}, "aml.run", "Ran the AML export")


def test_list_deserializes_and_queries_newest_first() -> None:
    items = [
        {
            "id": {"S": "1"},
            "timestamp": {"S": "2026-07-13T00:00:00+00:00"},
            "actor_email": {"S": "a@b.ca"},
            "actor_sub": {"S": "s"},
            "action": {"S": "matter.search"},
            "summary": {"S": "did a thing"},
            "status": {"S": "success"},
            "target": {"S": "M1"},
            "sources": {"L": [{"S": "clio"}, {"S": "graph"}]},
            "detail": {"S": '{"mode": "full"}'},
        }
    ]
    ddb = FakeDynamo(items=items)
    out = ActivityLog("t", client=ddb).list(limit=25)
    assert out[0]["action"] == "matter.search"
    assert out[0]["actor_email"] == "a@b.ca"
    assert out[0]["sources"] == ["clio", "graph"]
    assert out[0]["detail"] == {"mode": "full"}
    q = ddb.query_calls[0]
    assert q["ScanIndexForward"] is False
    assert q["Limit"] == 25


def test_list_clamps_limit() -> None:
    ddb = FakeDynamo()
    ActivityLog("t", client=ddb).list(limit=99999)
    assert ddb.query_calls[0]["Limit"] == 1000
