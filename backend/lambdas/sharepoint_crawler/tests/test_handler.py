"""Unit tests for the crawler handler's deadline + self-continue logic."""

from __future__ import annotations

import json
from typing import Any

from backend.lambdas.sharepoint_crawler import handler as h


def test_deadline_from_context_leaves_safety_margin() -> None:
    class Ctx:
        def get_remaining_time_in_millis(self) -> int:
            return 900_000  # 15 min

    dl = h._deadline_from(Ctx())
    # ~ now + 900s - 90s safety
    assert dl is not None
    import time

    assert 700 < (dl - time.monotonic()) < 820


def test_deadline_from_none_context_is_unbounded() -> None:
    assert h._deadline_from(None) is None


class FakeLambda:
    def __init__(self) -> None:
        self.invokes: list[dict[str, Any]] = []

    def invoke(self, **kw: Any) -> dict[str, Any]:
        self.invokes.append(kw)
        return {"StatusCode": 202}


class FakeBoto:
    def __init__(self, lam: FakeLambda) -> None:
        self._lam = lam

    def client(self, service: str, **_: Any) -> Any:
        assert service == "lambda"
        return self._lam


def test_self_continue_invokes_with_incremented_counter(monkeypatch) -> None:
    monkeypatch.setenv("APHRODITE_SELF_FUNCTION", "aphrodite-sharepoint-crawler")
    lam = FakeLambda()
    ok = h._self_continue(FakeBoto(lam), {"continues": 2})
    assert ok is True
    assert len(lam.invokes) == 1
    payload = json.loads(lam.invokes[0]["Payload"])
    assert payload == {"continues": 3}
    assert lam.invokes[0]["InvocationType"] == "Event"


def test_self_continue_no_function_env(monkeypatch) -> None:
    monkeypatch.delenv("APHRODITE_SELF_FUNCTION", raising=False)
    lam = FakeLambda()
    assert h._self_continue(FakeBoto(lam), {}) is False
    assert lam.invokes == []


def test_self_continue_respects_cap(monkeypatch) -> None:
    monkeypatch.setenv("APHRODITE_SELF_FUNCTION", "fn")
    lam = FakeLambda()
    assert h._self_continue(FakeBoto(lam), {"continues": h._MAX_CONTINUES}) is False
    assert lam.invokes == []  # would exceed the cap → no re-invoke
