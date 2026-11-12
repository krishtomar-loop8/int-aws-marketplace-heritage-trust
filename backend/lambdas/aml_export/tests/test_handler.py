"""Unit tests for the AML export Lambda handler. Fake deps; no AWS/network."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from backend.lambdas.aml_export import handler as H
from backend.lambdas.aml_export import quality
from backend.lambdas.aml_export.handler import Deps
from backend.lambdas.aml_export.tests.test_export import FakeClio, FakeEstateably, _resource
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


@pytest.fixture(autouse=True)
def _reset_quality_config():
    # handler() configures the module-level quality config on every
    # invocation; don't let one test's resolved config leak into the next.
    yield
    quality.reset_config()


class FakeStorage:
    def __init__(self) -> None:
        self.bucket = "ht-outputs-test"
        self.s3 = None  # no designation-list bucket in tests
        self.puts: list[dict[str, Any]] = []

    def put_export(
        self, workbook_bytes: bytes, result: dict[str, Any], *, now: datetime
    ) -> dict[str, str]:
        self.puts.append({"bytes": workbook_bytes, "result": result, "now": now})
        month, day = now.strftime("%Y-%m"), now.strftime("%Y-%m-%d")
        return {
            "workbook": f"aml/{month}/AML_Name_Export_{day}.xlsx",
            "manifest": f"aml/{month}/manifest.json",
        }

    def load_prior_matter_numbers(self, now: datetime) -> set[str]:
        return set()


def _install(monkeypatch, estateably, clio, storage, config=None):
    notifications: list[tuple[dict[str, str], dict[str, Any]]] = []
    deps = Deps(
        estateably=estateably,
        clio=clio,
        storage=storage,
        notify=lambda keys, result: notifications.append((keys, result)),
        config=config,
    )
    monkeypatch.setattr(H, "_DEPS", deps)
    return notifications


def _estate_with_intake(es, eid, title, first, last, number=None):
    attrs = {"title": title, "status": "Open"}
    if number:
        attrs["estateNumber"] = number
    es.estates.append(_resource(eid, attrs))
    es.intakes[eid] = {"attributes": {"firstName": first, "lastName": last}}


def test_handler_writes_workbook_and_notifies(monkeypatch):
    es = FakeEstateably()
    _estate_with_intake(es, "E1", "Estate of Margaret Whitfield - 190101", "Margaret", "Whitfield")
    _estate_with_intake(es, "E2", "Jane Doe Family Trust - 200002", "Jane", "Doe")
    storage = FakeStorage()
    notifications = _install(monkeypatch, es, FakeClio(), storage)

    result = H.handler({})

    assert result["bucket"] == "ht-outputs-test"
    assert len(storage.puts) == 1
    put_result = storage.puts[0]["result"]
    assert {p["party_role"] for p in put_result["parties"]} == {"Deceased", "Settlor"}
    assert result["manifest"].endswith("manifest.json")
    assert len(notifications) == 1
    assert notifications[0][0]["workbook"] == result["key"]


def test_handler_writes_when_no_active_estates(monkeypatch):
    storage = FakeStorage()
    notifications = _install(monkeypatch, FakeEstateably(), FakeClio(), storage)

    H.handler({})

    empty = storage.puts[0]["result"]
    assert empty["parties"] == [] and empty["excluded"] == []
    assert len(notifications) == 1


def test_handler_isolates_a_failing_estate_without_silently_dropping_it(monkeypatch):
    es = FakeEstateably()
    _estate_with_intake(es, "E1", "Estate of Margaret", "Margaret", "W", number="190101")
    _estate_with_intake(es, "E2", "Estate of John Roe", "John", "Roe", number="200003")
    es.raise_for_estate_id = "E1"
    storage = FakeStorage()
    _install(monkeypatch, es, FakeClio(), storage)

    H.handler({})

    result = storage.puts[0]["result"]
    assert {p["matter_number"] for p in result["parties"]} == {"200003"}
    # The failing estate is surfaced on Excluded Matters, never silently dropped.
    assert any("Could not process" in e["reason"] for e in result["excluded"])


def test_handler_ignores_event_payload(monkeypatch):
    es = FakeEstateably()
    _estate_with_intake(es, "E1", "Estate of Margaret - 190101", "Margaret", "W")
    storage = FakeStorage()
    _install(monkeypatch, es, FakeClio(), storage)

    result = H.handler({"unexpected": "payload"}, context=object())

    assert result["bucket"] == "ht-outputs-test"
    assert len(storage.puts[0]["result"]["parties"]) == 1


def test_org_config_from_env_malformed_json_falls_back(monkeypatch):
    monkeypatch.setenv("APHRODITE_OWN_COMPANY_NAMES", "{not valid json")
    monkeypatch.setenv("APHRODITE_INTERNAL_TITLE_KEYWORDS", "{not valid json")
    config = H._org_config_from_env(None)
    assert config.own_company_names == quality.OrgConfig().own_company_names
    assert config.internal_title_keywords == quality.OrgConfig().internal_title_keywords


def test_org_config_from_env_applies_overrides(monkeypatch):
    monkeypatch.setenv("APHRODITE_OWN_COMPANY_NAMES", '["Acme Trust"]')
    monkeypatch.setenv("APHRODITE_INTERNAL_MATTER_NUMBER_PATTERN", r"^\s*9999-\d+")
    config = H._org_config_from_env(None)
    assert config.own_company_names == ("Acme Trust",)
    assert config.internal_number_pattern == r"^\s*9999-\d+"
    # Untouched field keeps its own default.
    assert config.internal_title_keywords == quality.OrgConfig().internal_title_keywords


def test_org_config_from_env_empty_list_excludes_nothing(monkeypatch):
    monkeypatch.setenv("APHRODITE_OWN_COMPANY_NAMES", "[]")
    config = H._org_config_from_env(None)
    assert config.own_company_names == ()


def test_org_config_from_env_blank_keyword_entries_filtered(monkeypatch):
    monkeypatch.setenv("APHRODITE_INTERNAL_TITLE_KEYWORDS", '["suspense", "  ", ""]')
    config = H._org_config_from_env(None)
    assert config.internal_title_keywords == ("suspense",)


def test_org_config_from_env_prefers_configured_own_company_names(monkeypatch):
    monkeypatch.setenv("APHRODITE_OWN_COMPANY_NAMES", '["Env Co"]')
    config = ConfigClient(client=_FakeSsm())
    config.put({"own_company_names": ["Configured Co"]})
    assert H._org_config_from_env(config).own_company_names == ("Configured Co",)


def test_warm_container_picks_up_a_config_change_between_invocations(monkeypatch):
    # An admin runs the export, fixes own_company_names in the Deployment
    # Config tab (served by a different container), and re-runs: the re-run
    # on this same warm container (cached _DEPS) must use the new value.
    ssm = _FakeSsm()
    ConfigClient(client=ssm).put({"own_company_names": ["Unrelated Co"]})
    es = FakeEstateably()
    _estate_with_intake(es, "E1", "Estate of John Roe", "John", "Roe", number="200003")
    storage = FakeStorage()
    _install(monkeypatch, es, FakeClio(), storage, config=ConfigClient(client=ssm))

    H.handler({})
    assert [p["matter_number"] for p in storage.puts[0]["result"]["parties"]] == ["200003"]

    ConfigClient(client=ssm).put({"own_company_names": ["John Roe"]})  # another container
    H.handler({})
    assert storage.puts[1]["result"]["parties"] == []
