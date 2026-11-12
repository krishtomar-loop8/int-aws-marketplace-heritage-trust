"""Unit tests for the quarterly-report Lambda handler. Fake deps; no AWS."""

from __future__ import annotations

from datetime import date

from backend.lambdas.quarterly_report import handler as H
from backend.lambdas.quarterly_report.generator import ReportDataError
from backend.lambdas.quarterly_report.handler import Deps
from backend.lambdas.quarterly_report.period import ReportingPeriod
from backend.lambdas.quarterly_report.render_docx import LetterheadConfig
from backend.lambdas.quarterly_report.tests.test_render import _model
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


class FakeGenerator:
    def __init__(self, *, fail_for: set[str] | None = None) -> None:
        self.fail_for = fail_for or set()
        self.calls: list[tuple[str, ReportingPeriod]] = []

    def build(self, matter_id, period, *, letter_date):
        self.calls.append((str(matter_id), period))
        if str(matter_id) in self.fail_for:
            raise ReportDataError(f"no data for {matter_id}")
        model = _model()
        model.matter_number = str(matter_id)
        return model


class FakeStorage:
    def __init__(self) -> None:
        self.saved: list[str] = []

    def prefix(self, model):
        return f"quarterly/{model.matter_number}/{model.period.code}"

    def put_report(self, model, docx, pdf, *, generated_at):
        self.saved.append(model.matter_number)
        return {"cover_letter": f"{self.prefix(model)}/cover-letter.docx"}


class FakeDelivery:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.delivered: list[str] = []

    def deliver(self, model, docx, pdf):
        if self.fail:
            raise RuntimeError("sharepoint down")
        self.delivered.append(model.matter_number)
        return f"https://sp/.../{model.matter_number}"


def _install(
    monkeypatch, generator, storage, *, delivery=None, sleep=None, delay=0.0, **deps_kwargs
):
    notifications = []
    extra = {"sleep": sleep} if sleep is not None else {}
    deps = Deps(
        generator=generator,
        storage=storage,
        notify=lambda p, r, flagged=None: notifications.append((p, r, flagged)),
        delivery=delivery,
        delay_seconds=delay,
        **extra,
        **deps_kwargs,
    )
    monkeypatch.setattr(H, "_DEPS", deps)
    return notifications


def test_resolve_period_default_is_previous_quarter():
    assert H._resolve_period({}, today=date(2026, 5, 11)) == ReportingPeriod(2026, 1)


def test_resolve_period_honors_event_override():
    period = H._resolve_period({"period": {"year": 2025, "quarter": 3}}, today=date(2026, 5, 11))
    assert period == ReportingPeriod(2025, 3)


def test_resolve_matter_ids_from_event_and_env(monkeypatch):
    assert H._resolve_matter_ids({"matter_ids": [1, 2]}) == ["1", "2"]
    monkeypatch.setenv("APHRODITE_QUARTERLY_MATTER_IDS", "[10, 11]")
    assert H._resolve_matter_ids({}) == ["10", "11"]


def test_resolve_letter_date():
    assert H._resolve_letter_date({"letter_date": "2026-05-11"}, today=date(2026, 6, 1)) == date(
        2026, 5, 11
    )
    assert H._resolve_letter_date({}, today=date(2026, 6, 1)) == date(2026, 6, 1)


def test_handler_generates_each_matter_and_notifies(monkeypatch):
    gen, store = FakeGenerator(), FakeStorage()
    notifications = _install(monkeypatch, gen, store)

    result = H.handler({"matter_ids": ["100001", "100002"], "period": {"year": 2026, "quarter": 1}})

    assert result["period"] == "2026-Q1"
    assert set(result["generated"]) == {"100001", "100002"}
    assert result["failed"] == []
    assert store.saved == ["100001", "100002"]
    assert len(notifications) == 1


def test_handler_isolates_per_matter_failure(monkeypatch):
    gen = FakeGenerator(fail_for={"100002"})
    store = FakeStorage()
    _install(monkeypatch, gen, store)

    result = H.handler(
        {"matter_ids": ["100001", "100002", "100003"], "period": {"year": 2026, "quarter": 1}}
    )

    assert set(result["generated"]) == {"100001", "100003"}
    assert [f["matter"] for f in result["failed"]] == ["100002"]
    # The good matters were still written despite the failure in the middle.
    assert store.saved == ["100001", "100003"]


def test_handler_noop_when_no_matters(monkeypatch):
    monkeypatch.delenv("APHRODITE_QUARTERLY_MATTER_IDS", raising=False)
    notifications = _install(monkeypatch, FakeGenerator(), FakeStorage())
    result = H.handler({})
    assert result["skipped"] == "no_matters"
    assert result["generated"] == []
    assert notifications == []


def test_scheduled_run_delivers_to_sharepoint(monkeypatch):
    delivery = FakeDelivery()
    _install(monkeypatch, FakeGenerator(), FakeStorage(), delivery=delivery)

    result = H.handler(
        {
            "sharepoint": True,
            "matter_ids": ["100001", "100002"],
            "period": {"year": 2026, "quarter": 1},
        }
    )

    assert delivery.delivered == ["100001", "100002"]
    assert set(result["sharepoint_uploaded"]) == {"100001", "100002"}


def test_on_demand_run_does_not_deliver(monkeypatch):
    delivery = FakeDelivery()
    _install(monkeypatch, FakeGenerator(), FakeStorage(), delivery=delivery)

    # No "sharepoint" flag ⇒ on-demand portal path ⇒ S3 only.
    result = H.handler({"matter_ids": ["100001"], "period": {"year": 2026, "quarter": 1}})

    assert delivery.delivered == []
    assert result["sharepoint_uploaded"] == []


def test_delivery_failure_keeps_s3_draft(monkeypatch):
    store = FakeStorage()
    _install(monkeypatch, FakeGenerator(), store, delivery=FakeDelivery(fail=True))

    result = H.handler(
        {"sharepoint": True, "matter_ids": ["100001"], "period": {"year": 2026, "quarter": 1}}
    )

    # The S3 draft still succeeded; only the SharePoint copy failed.
    assert result["generated"] == ["100001"]
    assert store.saved == ["100001"]
    assert result["sharepoint_uploaded"] == []


def test_inter_matter_delay_uses_injected_sleeper(monkeypatch):
    sleeps: list[float] = []
    _install(
        monkeypatch, FakeGenerator(), FakeStorage(), sleep=lambda s: sleeps.append(s), delay=1.2
    )

    H.handler({"matter_ids": ["a", "b", "c"], "period": {"year": 2026, "quarter": 1}})

    assert sleeps == [1.2, 1.2]  # paced between matters: n-1 sleeps, none before the first


# -- practice-area discovery (scheduled runs) --------------------------------


class FakeClioList:
    def __init__(self, matters):
        self._matters = matters

    def list_matters(self, *, status="open", fields=None, limit=500):
        return list(self._matters)


def _matter(id, number, area=None):
    m = {"id": id, "display_number": number, "client": {"name": f"Client {id}"}}
    if area is not None:
        m["practice_area"] = {"name": area}
    return m


_DISCOVER_CFG = {
    "qualifying_practice_areas": ("Trustee", "Committee", "Power of Attorney"),
    "exclusion_field": "Report Exclusion",
}


def test_discover_selects_qualifying_and_flags_uncategorized(monkeypatch):
    gen, store = FakeGenerator(), FakeStorage()
    clio = FakeClioList(
        [
            _matter(1, "250001", "Trustee"),
            _matter(2, "250002", "Executor/Administrator"),  # excluded
            _matter(3, "250003"),  # no practice area, no flag → flagged
        ]
    )
    notifications = _install(monkeypatch, gen, store, clio=clio, **_DISCOVER_CFG)

    result = H.handler({"discover": True, "period": {"year": 2026, "quarter": 2}})

    assert result["generated"] == ["1"]
    assert store.saved == ["1"]
    assert [r["matter_number"] for r in result["flagged_no_practice_area"]] == ["250003"]
    # notify received the flagged list as its third arg
    _, _, flagged = notifications[0]
    assert [r.number for r in flagged] == ["250003"]


def test_discover_only_lists_without_generating(monkeypatch):
    gen, store = FakeGenerator(), FakeStorage()
    clio = FakeClioList([_matter(1, "250001", "Trustee"), _matter(2, "250002", "Miscellaneous")])
    notifications = _install(
        monkeypatch, gen, store, clio=clio,
        qualifying_practice_areas=("Trustee",), exclusion_field="Report Exclusion",
    )

    result = H.handler({"discover_only": True})

    assert result["mode"] == "discover_only"
    assert [r["matter_number"] for r in result["selected"]] == ["250001"]
    assert gen.calls == []  # nothing generated
    assert store.saved == []
    assert notifications == []  # a dry run notifies no one


def test_explicit_matter_ids_bypass_discovery(monkeypatch):
    gen, store = FakeGenerator(), FakeStorage()
    clio = FakeClioList([_matter(99, "259999", "Trustee")])
    _install(monkeypatch, gen, store, clio=clio, **_DISCOVER_CFG)

    # Both a discover flag and explicit ids present ⇒ explicit ids win, no discovery.
    result = H.handler(
        {"discover": True, "matter_ids": ["100001"], "period": {"year": 2026, "quarter": 2}}
    )

    assert result["generated"] == ["100001"]
    assert "flagged_no_practice_area" not in result


def test_letterhead_from_env_partial_override(monkeypatch):
    monkeypatch.setenv("APHRODITE_LETTERHEAD_NAME", "Acme Trust")
    monkeypatch.delenv("APHRODITE_LETTERHEAD_ADDRESS", raising=False)
    letterhead = H._letterhead_from_env(None)
    assert letterhead.name == "Acme Trust"
    assert letterhead.address == LetterheadConfig().address  # untouched sibling field


def test_letterhead_from_env_prefers_configured_value(monkeypatch):
    monkeypatch.setenv("APHRODITE_LETTERHEAD_NAME", "Env Name")
    config = ConfigClient(client=_FakeSsm())
    config.put({"letterhead_name": "Configured Name"})
    letterhead = H._letterhead_from_env(config)
    assert letterhead.name == "Configured Name"


def test_letterhead_from_env_falls_back_when_config_client_is_none(monkeypatch):
    monkeypatch.setenv("APHRODITE_LETTERHEAD_NAME", "Env Name")
    assert H._letterhead_from_env(None).name == "Env Name"


def test_warm_container_picks_up_a_config_change_between_invocations(monkeypatch):
    # The portal can invoke this Lambda on demand in quick succession: a
    # config change saved in between (by a different container) must apply to
    # the next run on this same warm container (cached _DEPS) — practice
    # areas, the exclusion field, and the letterhead alike.
    ssm = _FakeSsm()
    ConfigClient(client=ssm).put(
        {
            "quarterly_qualifying_practice_areas": ["Trustee"],
            "quarterly_exclusion_field": "First Flag",
            "letterhead_name": "First Co",
        }
    )
    trustee = _matter(1, "250001", "Trustee")
    committee = _matter(2, "250002", "Committee")
    flagged = _matter(3, "250003", "Committee")
    flagged["custom_field_values"] = [{"id": 5, "value": True, "field_name": "Second Flag"}]
    clio = FakeClioList([trustee, committee, flagged])
    gen, store = FakeGenerator(), FakeStorage()
    _install(monkeypatch, gen, store, clio=clio, config=ConfigClient(client=ssm))
    letterheads: list[str] = []
    monkeypatch.setattr(
        H, "render_cover_letter", lambda model, letterhead: letterheads.append(letterhead.name)
    )
    monkeypatch.setattr(H, "render_trust_ledger", lambda model: b"")

    first = H.handler({"discover": True, "period": {"year": 2026, "quarter": 2}})
    ConfigClient(client=ssm).put(  # saved via another container
        {
            "quarterly_qualifying_practice_areas": ["Committee"],
            "quarterly_exclusion_field": "Second Flag",
            "letterhead_name": "Second Co",
        }
    )
    second = H.handler({"discover": True, "period": {"year": 2026, "quarter": 2}})

    assert first["generated"] == ["1"]  # FakeGenerator reports the matter id
    assert second["generated"] == ["2"]  # matter 3 excluded by the new flag field
    assert letterheads == ["First Co", "Second Co"]
