"""Tests for Tier-0 quick-answer handlers (fake connectors; no network)."""

from __future__ import annotations

from typing import Any

from backend.search.quick_answers import QuickAnswerService
from backend.search.resolve import MatterCandidate, SourceRef


def _candidate(*, estateably: str | None = "E1", clio: str | None = "C1") -> MatterCandidate:
    refs = []
    if estateably:
        refs.append(SourceRef("estateably", estateably, "Estate of Test"))
    if clio:
        refs.append(SourceRef("clio", clio, "00001-Test"))
    return MatterCandidate(name="Estate of Test", refs=refs, matter_type="estate")


class FakeEstateably:
    base_url = "https://example.app.estateably.com"

    def __init__(self, **data: Any) -> None:
        self._data = data

    def _ret(self, key: str, default: Any) -> Any:
        val = self._data.get(key, default)
        if isinstance(val, Exception):
            raise val
        return val

    def get_estate_summary(self, estate_id: str) -> Any:
        return self._ret(
            "summary", {"attributes": {"balance": 171558.76, "estateValue": 2319776.67}}
        )

    def get_client_intake(self, estate_id: str) -> Any:
        return self._ret("intake", {"attributes": {"firstName": "DAVID"}})

    def list_beneficiaries(self, estate_id: str) -> Any:
        return self._ret(
            "beneficiaries",
            [{"id": "b1", "attributes": {"name": "Julie Scott", "familyMemberType": "Step-child"}}],
        )

    def list_executors(self, estate_id: str) -> Any:
        return self._ret(
            "executors", [{"id": "x1", "attributes": {"name": "Aphrodite"}}]
        )

    def list_documents(self, estate_id: str) -> Any:
        return self._ret(
            "documents", [{"id": "d1", "attributes": {"name": "SPARKS David signed Will.pdf"}}]
        )

    def list_tasks(self, estate_id: str) -> Any:
        return self._ret(
            "tasks", [{"id": "t1", "attributes": {"name": "File T3", "dueDate": "2099-03-31"}}]
        )


class FakeClio:
    base_url = "https://app.clio.com/api/v4"

    def __init__(self, **data: Any) -> None:
        self._data = data

    def get_matter_account_balances(self, matter_id: Any) -> Any:
        return self._data.get(
            "matter_balances",
            [{"id": 9, "name": "Trust", "type": "Trust", "balance": 5000.0}],
        )

    def list_calendar_entries(self, matter_id: Any, *, fields: str | None = None) -> Any:
        return self._data.get("calendar", [])

    def list_bills(self, matter_id: Any, *, fields: str | None = None) -> Any:
        return self._data.get("bills", [])


def test_balance_from_estateably_summary() -> None:
    svc = QuickAnswerService(estateably=FakeEstateably(), clio=FakeClio(matter_balances=[]))
    ans = svc.answer("balance", _candidate())
    assert ans is not None
    assert "$171,558.76" in ans.text
    assert any(c["system"] == "estateably" and c["record_id"] == "E1" for c in ans.citations)
    assert "(estateably:E1)" in ans.text  # inline marker for the footnote


def test_parties_lists_beneficiaries_and_executors() -> None:
    svc = QuickAnswerService(estateably=FakeEstateably())
    ans = svc.answer("parties", _candidate())
    assert ans is not None
    assert "Julie Scott" in ans.text
    assert "Aphrodite" in ans.text


def test_kyc_reports_intake_and_doc_counts() -> None:
    svc = QuickAnswerService(estateably=FakeEstateably())
    ans = svc.answer("kyc", _candidate())
    assert ans is not None
    assert "Client intake record: present" in ans.text
    # the Will.pdf shouldn't count as a KYC/ID doc; count line present
    assert "Parties requiring KYC" in ans.text


def test_deadlines_filters_future_and_sorts() -> None:
    svc = QuickAnswerService(estateably=FakeEstateably(), clio=FakeClio())
    ans = svc.answer("deadlines", _candidate())
    assert ans is not None
    assert "2099-03-31" in ans.text


def test_declines_when_source_missing() -> None:
    # No estateably ref → parties/kyc/documents decline (→ Tier 2).
    svc = QuickAnswerService(estateably=FakeEstateably(), clio=FakeClio())
    assert svc.answer("parties", _candidate(estateably=None)) is None
    assert svc.answer("documents", _candidate(estateably=None)) is None


def test_declines_when_source_errors() -> None:
    svc = QuickAnswerService(
        estateably=FakeEstateably(summary=RuntimeError("402")), clio=FakeClio(matter_balances=[])
    )
    # balance's only sources fail/empty → decline to Tier 2
    assert svc.answer("balance", _candidate()) is None


def test_unknown_intent_returns_none() -> None:
    svc = QuickAnswerService(estateably=FakeEstateably())
    assert svc.answer("nonsense", _candidate()) is None


_INTAKE = {
    "attributes": {
        "firstName": "Oonagh",
        "firstMiddleName": "Geraldine",
        "lastName": "McDonald",
        "birthDate": "1926-10-17",
        "dateOfDeath": "2020-05-16",
        "maritalStatus": "single",
        "province": "british columbia",
        "sinNumber": "123456789",  # must never surface
    }
}


def test_profile_leads_with_dob_and_hides_sin() -> None:
    svc = QuickAnswerService(estateably=FakeEstateably(intake=_INTAKE))
    ans = svc.answer("profile", _candidate(), query="what's the date of birth of Oonagh McDonald?")
    assert ans is not None
    assert "Date of birth of Oonagh Geraldine McDonald: 1926-10-17" in ans.text
    assert "123456789" not in ans.text  # SIN deliberately excluded
    assert "Date of death: 2020-05-16" in ans.text  # supporting facts still listed
    assert any(c["system"] == "estateably" and c["record_id"] == "E1" for c in ans.citations)


def test_profile_leads_with_dod_for_death_query() -> None:
    svc = QuickAnswerService(estateably=FakeEstateably(intake=_INTAKE))
    ans = svc.answer("profile", _candidate(), query="when did Oonagh McDonald die?")
    assert ans is not None
    assert "Date of death of Oonagh Geraldine McDonald: 2020-05-16" in ans.text


def test_profile_declines_when_intake_missing() -> None:
    svc = QuickAnswerService(estateably=FakeEstateably(intake=None))
    assert svc.answer("profile", _candidate(), query="date of birth") is None


class _FakeGraph:
    def __init__(self, by_mailbox: dict[str, list[dict[str, Any]]]) -> None:
        self._by = by_mailbox

    def list_users(self, *, top: int | None = None) -> list[dict[str, Any]]:
        return [{"id": "u1", "mail": "a@ht.ca", "accountEnabled": True}]

    def search_mailbox(self, user_id: str, kql: str, *, top: int = 25) -> list[dict[str, Any]]:
        return self._by.get(user_id, [])


def _mail(mid: str, subject: str, addr: str, *, to: list[str] | None = None) -> dict[str, Any]:
    m: dict[str, Any] = {
        "id": mid,
        "subject": subject,
        "from": {"emailAddress": {"address": addr}},
        "receivedDateTime": "2026-07-05T10:00:00Z",
        "webLink": f"https://outlook/{mid}",
    }
    if to is not None:
        m["toRecipients"] = [{"emailAddress": {"address": a}} for a in to]
    return m


def test_correspondence_excludes_loop8_dev_emails() -> None:
    graph = _FakeGraph(
        {
            "u1": [
                _mail("real", "McDonald probate update", "counsel@scionlaw.ca"),
                _mail("dev-from", "New AI tool is live", "team@loop8.io"),
                _mail("dev-to", "RE: McDonald test", "nicole@ht.ca", to=["dev@loop8.io"]),
            ]
        }
    )
    svc = QuickAnswerService(graph=graph)
    ans = svc.answer("correspondence", _candidate(), term="mcdonald", query="recent emails")
    assert ans is not None
    assert "McDonald probate update" in ans.text
    assert "loop8.io" not in ans.text
    assert all(c["record_id"] not in {"dev-from", "dev-to"} for c in ans.citations)
