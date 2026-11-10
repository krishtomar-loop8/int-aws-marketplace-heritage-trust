"""Unit tests for the integration health store + suggested-fix mapping.

No AWS: DynamoDB is a small in-memory fake that implements the get/put/query
surface the store uses (single partition, begins_with on the sort key).
"""

from __future__ import annotations

from typing import Any

from backend.api.integrations import IntegrationHealthStore, suggest_fix


class FakeDdb:
    """Minimal in-memory DynamoDB stand-in keyed by (pk, sk)."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def put_item(self, *, TableName: str, Item: dict[str, Any]) -> None:
        self.items[(Item["pk"]["S"], Item["sk"]["S"])] = Item

    def get_item(self, *, TableName: str, Key: dict[str, Any]) -> dict[str, Any]:
        item = self.items.get((Key["pk"]["S"], Key["sk"]["S"]))
        return {"Item": item} if item else {}

    def query(
        self,
        *,
        TableName: str,
        KeyConditionExpression: str,
        ExpressionAttributeValues: dict[str, Any],
        ScanIndexForward: bool = True,
        Limit: int | None = None,
    ) -> dict[str, Any]:
        pk = ExpressionAttributeValues[":p"]["S"]
        prefix = next(
            (
                ExpressionAttributeValues[k]["S"]
                for k in (":c", ":l")
                if k in ExpressionAttributeValues
            ),
            None,
        )
        rows = [
            item
            for (p, s), item in self.items.items()
            if p == pk and (prefix is None or s.startswith(prefix))
        ]
        rows.sort(key=lambda it: it["sk"]["S"], reverse=not ScanIndexForward)
        if Limit is not None:
            rows = rows[:Limit]
        return {"Items": rows}


class Clock:
    """Monotonic clock so ISO timestamps (and thus sort keys) are deterministic."""

    def __init__(self, start: float = 1_700_000_000.0, step: float = 3600.0) -> None:
        self.t = start
        self.step = step

    def __call__(self) -> float:
        v = self.t
        self.t += self.step
        return v


def _store() -> IntegrationHealthStore:
    return IntegrationHealthStore(table="t", client=FakeDdb(), clock=Clock())


# -- store: current status + counter ----------------------------------------


def test_record_success_snapshot_has_zero_failures() -> None:
    store = _store()
    store.record_result("clio", ok=True)
    snap = store.snapshot()
    assert snap["alerting"] is False
    row = snap["integrations"][0]
    assert row["integration"] == "clio"
    assert row["ok"] is True
    assert row["consecutive_failures"] == 0


def test_snapshot_enabled_filter_drops_a_stale_disabled_connector_row() -> None:
    # A connector disabled after this failure was recorded (Phase 2 modular
    # architecture) must not keep alerting — nothing probes it any more.
    store = _store()
    store.record_result("estateably", ok=False, detail="boom")
    store.record_result("estateably", ok=False, detail="boom")
    store.record_result("clio", ok=True)
    snap = store.snapshot(enabled=frozenset({"clio"}))
    assert {row["integration"] for row in snap["integrations"]} == {"clio"}
    assert snap["alerting"] is False


def test_consecutive_failures_increment_and_reset() -> None:
    store = _store()
    store.record_result("clio", ok=True)
    store.record_result("clio", ok=False, detail="boom", suggested_fix="fix it")
    assert store.snapshot()["integrations"][0]["consecutive_failures"] == 1
    store.record_result("clio", ok=False, detail="boom")
    snap = store.snapshot()
    assert snap["integrations"][0]["consecutive_failures"] == 2
    assert snap["integrations"][0]["suggested_fix"] == ""  # last write carried no fix
    # Two failures in a row → the UI alert fires.
    assert snap["alerting"] is True
    # Recovery resets the counter and clears the alert.
    store.record_result("clio", ok=True)
    snap = store.snapshot()
    assert snap["integrations"][0]["consecutive_failures"] == 0
    assert snap["alerting"] is False


def test_transition_log_written_only_on_state_flip() -> None:
    store = _store()
    store.record_result("clio", ok=True)  # first observation → no transition
    store.record_result("clio", ok=True)  # no flip → no transition
    store.record_result("clio", ok=False, detail="down")  # up→down → 1 transition
    store.record_result("clio", ok=False, detail="still down")  # no flip
    store.record_result("clio", ok=True)  # down→up → 2nd transition
    log = store.snapshot()["log"]
    assert len(log) == 2
    # Newest first: the recovery, then the outage.
    assert log[0]["ok"] is True
    assert log[1]["ok"] is False
    assert log[1]["detail"] == "down"


def test_snapshot_sorts_and_isolates_integrations() -> None:
    store = _store()
    store.record_result("quickbooks", ok=False, detail="401")
    store.record_result("quickbooks", ok=False, detail="401")
    store.record_result("clio", ok=True)
    snap = store.snapshot()
    names = [r["integration"] for r in snap["integrations"]]
    assert names == ["clio", "quickbooks"]  # sorted, both present
    by_name = {r["integration"]: r for r in snap["integrations"]}
    assert by_name["clio"]["ok"] is True and by_name["clio"]["consecutive_failures"] == 0
    assert by_name["quickbooks"]["consecutive_failures"] == 2
    assert snap["alerting"] is True  # one down integration is enough


# -- suggested-fix mapping ---------------------------------------------------


def test_suggest_fix_quickbooks_token_is_reauthorize() -> None:
    msg = suggest_fix("quickbooks", "HttpError: QuickBooks token refresh failed: 400")
    assert "re-authorize" in msg.lower()
    assert "quickbooks" in msg.lower()


def test_suggest_fix_billing_hold() -> None:
    assert "402" in suggest_fix("estateably", "HttpError: 402 Payment Required")


def test_suggest_fix_forbidden() -> None:
    assert "403" in suggest_fix("graph", "HttpError: 403 Forbidden")


def test_suggest_fix_timeout_is_transient() -> None:
    assert "transient" in suggest_fix("clio", "ReadTimeout: read timed out").lower()


def test_suggest_fix_server_error() -> None:
    assert "5xx" in suggest_fix("clio", "HttpError: 503 ServiceUnavailable")


def test_suggest_fix_default() -> None:
    msg = suggest_fix("clio", "ValueError: something odd")
    assert "secrets manager" in msg.lower()


# -- on_alert (threshold-based alerting) -------------------------------------


def test_alert_fires_at_threshold_not_before() -> None:
    calls: list[tuple[str, str]] = []
    store = IntegrationHealthStore(
        table="t",
        client=FakeDdb(),
        clock=Clock(),
        on_alert=lambda name, detail: calls.append((name, detail)),
    )
    store.record_result("clio", ok=False, detail="boom")  # 1st failure
    assert calls == []
    store.record_result("clio", ok=False, detail="boom")  # 2nd failure (ALERT_THRESHOLD)
    assert calls == [("clio", "boom")]


def test_alert_not_resent_on_subsequent_failures() -> None:
    calls: list[tuple[str, str]] = []
    store = IntegrationHealthStore(
        table="t",
        client=FakeDdb(),
        clock=Clock(),
        on_alert=lambda name, detail: calls.append((name, detail)),
    )
    for _ in range(4):
        store.record_result("clio", ok=False, detail="boom")
    assert len(calls) == 1


def test_alert_not_fired_on_recovery() -> None:
    calls: list[tuple[str, str]] = []
    store = IntegrationHealthStore(
        table="t",
        client=FakeDdb(),
        clock=Clock(),
        on_alert=lambda name, detail: calls.append((name, detail)),
    )
    store.record_result("clio", ok=False, detail="boom")
    store.record_result("clio", ok=False, detail="boom")
    store.record_result("clio", ok=True)
    assert len(calls) == 1  # only the original alert, not a second one on recovery


def test_no_on_alert_configured_is_a_noop() -> None:
    store = IntegrationHealthStore(table="t", client=FakeDdb(), clock=Clock())  # on_alert omitted
    store.record_result("clio", ok=False, detail="boom")
    store.record_result("clio", ok=False, detail="boom")  # must not raise


def test_repeated_healthy_results_never_fire_alert() -> None:
    # Review Focus: re-submitting good credentials for an already-healthy
    # connector (repeated ok=True) must not crash or spuriously alert.
    calls: list[tuple[str, str]] = []
    store = IntegrationHealthStore(
        table="t",
        client=FakeDdb(),
        clock=Clock(),
        on_alert=lambda name, detail: calls.append((name, detail)),
    )
    store.record_result("clio", ok=True)
    store.record_result("clio", ok=True)
    store.record_result("clio", ok=True)
    assert calls == []
