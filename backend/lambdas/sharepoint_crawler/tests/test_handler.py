"""Unit tests for the crawler handler's deadline + self-continue logic."""

from __future__ import annotations

import json
from typing import Any

from backend.lambdas.sharepoint_crawler import handler as h
from backend.search import scoping
from services.config import ConfigClient


class _FakeSsm:
    def __init__(self):
        self.params = {}

    def get_parameter(self, *, Name):
        from botocore.exceptions import ClientError

        if Name not in self.params:
            raise ClientError(
                {"Error": {"Code": "ParameterNotFound", "Message": "not found"}},
                "GetParameter",
            )
        return {"Parameter": {"Value": self.params[Name]}}

    def put_parameter(self, *, Name, Value, Type, Overwrite):
        self.params[Name] = Value


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


def test_excluded_folders_from_env_applies_override(monkeypatch) -> None:
    monkeypatch.setenv("APHRODITE_EXCLUDED_SHAREPOINT_FOLDERS", '["payroll"]')
    assert h._excluded_folders_from_env(None) == frozenset({"payroll"})


def test_excluded_folders_from_env_malformed_json_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("APHRODITE_EXCLUDED_SHAREPOINT_FOLDERS", "{not valid json")
    assert h._excluded_folders_from_env(None) == scoping.DEFAULT_EXCLUDED_FOLDERS


def test_excluded_folders_from_env_unset_uses_default(monkeypatch) -> None:
    monkeypatch.delenv("APHRODITE_EXCLUDED_SHAREPOINT_FOLDERS", raising=False)
    assert h._excluded_folders_from_env(None) == scoping.DEFAULT_EXCLUDED_FOLDERS


def test_excluded_folders_from_env_empty_list_excludes_nothing(monkeypatch) -> None:
    monkeypatch.setenv("APHRODITE_EXCLUDED_SHAREPOINT_FOLDERS", "[]")
    assert h._excluded_folders_from_env(None) == frozenset()


def test_excluded_folders_from_env_blank_entries_filtered(monkeypatch) -> None:
    monkeypatch.setenv("APHRODITE_EXCLUDED_SHAREPOINT_FOLDERS", '["hr", "  ", ""]')
    assert h._excluded_folders_from_env(None) == frozenset({"hr"})


def test_excluded_folders_from_env_prefers_configured_value(monkeypatch) -> None:
    config = ConfigClient(client=_FakeSsm())
    config.put({"excluded_sharepoint_folders": ["legal"]})
    assert h._excluded_folders_from_env(config) == frozenset({"legal"})


def test_sharepoint_sites_prefers_configured_value(monkeypatch) -> None:
    monkeypatch.setenv("APHRODITE_SHAREPOINT_SITES", '["https://env.example"]')
    config = ConfigClient(client=_FakeSsm())
    config.put({"sharepoint_sites": ["https://configured.example"]})
    assert h._sharepoint_sites_from_env(config) == ["https://configured.example"]


def test_sharepoint_sites_falls_back_to_env_when_not_configured(monkeypatch) -> None:
    monkeypatch.setenv("APHRODITE_SHAREPOINT_SITES", '["https://env.example"]')
    assert h._sharepoint_sites_from_env(None) == ["https://env.example"]
