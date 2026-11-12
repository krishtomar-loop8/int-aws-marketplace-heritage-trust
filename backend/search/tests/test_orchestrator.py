"""Unit tests for the dossier builder. Connectors are faked; no AWS/network."""

from __future__ import annotations

import json
from typing import Any

import pytest

from backend.search import orchestrator as orch
from backend.search.orchestrator import (
    DossierBuilder,
    DossierLimits,
    _best_ref_ids,
    _matter_title,
)
from backend.search.resolve import MatterCandidate, SourceRef
from services.http import HttpError
from services.knowledge_base import KBResult


@pytest.fixture(autouse=True)
def _clear_shared_cache():
    """The module-level roster/bank-account cache persists across warm invocations;
    clear it around each test so fixtures don't leak between them."""
    orch._shared_cache.clear()
    yield
    orch._shared_cache.clear()


def _cand() -> MatterCandidate:
    # The Clio ref's matter id ("123") and its client-contact id ("CID9") are
    # deliberately different: QuickBooks matches on the client-contact id (in the
    # customer DisplayName), never the matter id.
    return MatterCandidate(
        name="Smith Estate",
        refs=[
            SourceRef("estateably", "E1", "Estate of Smith"),
            SourceRef("clio", "123", "Smith", qb_customer_id="CID9"),
        ],
        matter_type="estate",
    )


def est_item(id_: str, *, meta: dict[str, Any] | None = None, **attrs: Any) -> dict[str, Any]:
    item: dict[str, Any] = {"id": id_, "attributes": attrs}
    if meta is not None:
        item["meta"] = meta  # accounts carry their balance here, beside attributes
    return item


class FakeEstateably:
    """Returns canned data; each attribute can be an Exception to simulate failure.

    ``calls`` records the pull keys touched, in order, so tests can assert the
    per-case gate short-circuits the fan-out (only the summary canary runs).
    """

    def __init__(self, **overrides: Any) -> None:
        self._o = overrides
        self.calls: list[str] = []

    def _v(self, key: str, default: Any) -> Any:
        self.calls.append(key)
        val = self._o.get(key, default)
        if isinstance(val, Exception):
            raise val
        return val

    def get_estate_summary(self, _id):
        return self._v("summary", est_item("E1", stage="Administration"))

    def get_workflow(self, _id):
        return self._v("workflow", est_item("E1", nodes=[{"name": "Intake"}]))

    def get_client_intake(self, _id):
        return self._v("intake", est_item("E1", status="incomplete"))

    def list_documents(self, _id):
        return self._v("documents", [est_item("D1", name="Will.pdf")])

    def list_contacts(self, _id):
        return self._v("contacts", [est_item("C1", name="Jane")])

    def list_beneficiaries(self, _id):
        return self._v("beneficiaries", [est_item("B1", name="John")])

    def list_executors(self, _id):
        return self._v("executors", [est_item("X1", name="Pat")])

    def list_tasks(self, _id):
        return self._v("tasks", [est_item("T1", name="File T1", status="open")])

    def list_accounts(self, _id):
        return self._v(
            "accounts",
            [
                est_item(
                    "A1",
                    accountType="Estate Bank Account",
                    institution="RBC",
                    number="1234567890",
                    meta={"balance": {"value": 12500.5}},
                )
            ],
        )

    def list_inventory(self, _id):
        return self._v(
            "inventory",
            [
                est_item(
                    "I1",
                    inventoryType="GIC",
                    category="Asset",
                    value=50000,
                    fields={"transactionMoneyType": "Capital"},
                )
            ],
        )

    def list_comments(self, _id):
        return self._v("comments", [est_item("N1", body="called executor")])


class FakeClio:
    def __init__(self, **overrides: Any) -> None:
        self._o = overrides

    def _v(self, key: str, default: Any) -> Any:
        val = self._o.get(key, default)
        if isinstance(val, Exception):
            raise val
        return val

    def get_matter(self, _id, **_):
        return self._v(
            "matter",
            {"id": 123, "display_number": "00042", "description": "Smith", "status": "open"},
        )

    def list_tasks(self, _id):
        return self._v("tasks", [{"id": 9, "name": "Call bank", "status": "pending"}])

    def list_calendar_entries(self, _id):
        return self._v("calendar", [{"id": 5, "summary": "T1 deadline", "start_at": "2026-04-30"}])

    def list_matter_notes(self, _id):
        return self._v("notes", [{"id": 7, "detail": "opening note", "created_at": "2026-01-02"}])

    def list_bank_accounts(self, *, fields=None):
        self.bank_fields = fields
        return self._v("bank_accounts", [{"id": 1, "name": "Trust", "balance": 1000}])

    def get_matter_account_balances(self, _id):
        return self._v(
            "matter_balances",
            [{"id": 11, "name": "Main Trust Account", "type": "Trust", "balance": 38446.24}],
        )

    def list_bank_transactions(self):
        return self._v(
            "bank_transactions",
            [{"id": 2, "description": "deposit", "amount": 50, "date": "2026-02-01"}],
        )

    def list_bills(self, _id, *, fields=None):
        self.bill_fields = fields
        # Per-matter bills for client-level reconciliation tests; otherwise the shared
        # default (single-matter behaviour, unchanged).
        by_matter = self._o.get("bills_by_matter")
        if by_matter is not None:
            return by_matter.get(str(_id), [])
        return self._v(
            "bills", [{"id": 3, "number": "INV1", "total": 200, "paid": 200, "state": "paid"}]
        )

    def list_matters(self, *, client_id=None, status="open", fields=None, **_):
        # Default: no client matters → client-level reconciliation falls back to
        # matter-level (existing single-matter tests are unaffected).
        return self._v("client_matters", [])


# -- assembly ---------------------------------------------------------------


def test_build_assembles_sections_from_both_sources() -> None:
    d = DossierBuilder(estateably=FakeEstateably(), clio=FakeClio()).build(_cand())

    # Title comes from the longest label among the PULLED refs (here the estateably
    # "Estate of Smith"), not the fixture's synthetic candidate.name — see
    # test_matter_title_* for why.
    assert d.matter_name == "Estate of Smith"
    assert d.matter_type == "estate"
    assert set(d.sections) >= {
        "estate_summary",
        "kyc",
        "parties",
        "tasks",
        "financials",
        "activity",
        "deadlines",
    }
    # tasks come from BOTH systems
    systems = {i.system for i in d.section("tasks").items}
    assert systems == {"estateably", "clio"}
    assert not d.errors


def test_every_item_carries_provenance() -> None:
    d = DossierBuilder(estateably=FakeEstateably(), clio=FakeClio()).build(_cand())
    for sec in d.sections.values():
        for item in sec.items:
            assert item.system in {"estateably", "clio"}
            assert item.record_id  # non-empty


def test_clio_and_estateably_items_link_to_matter_and_estate() -> None:
    d = DossierBuilder(estateably=FakeEstateably(), clio=FakeClio()).build(_cand())
    real = [i for sec in d.sections.values() for i in sec.items if i.record_id != "-"]
    clio = [i for i in real if i.system == "clio"]
    est = [i for i in real if i.system == "estateably"]
    # _cand refs: clio id "123", estateably id "E1" → every datum links to its parent.
    assert clio and all(i.data.get("url") == "https://app.clio.com/nc/#/matters/123" for i in clio)
    assert est and all(i.data.get("url", "").endswith("/#/estates/E1/overview") for i in est)


def test_clio_matter_balances_render_and_firm_wide_accounts_stay_out() -> None:
    # The dossier carries the MATTER-scoped trust/operating balances
    # (Matter.account_balances) — the old firm-wide bank_accounts pull attached
    # the firm's pooled trust balance (eight figures) to every matter.
    clio = FakeClio(
        matter_balances=[
            {"id": 11, "name": "Main Trust Account", "type": "Trust", "balance": 38446.24},
            {"id": 12, "name": "Operating Account", "type": "Operating", "balance": 0.0},
        ]
    )
    d = DossierBuilder(clio=clio).build(_cand())
    fin = [i for i in d.section("financials").items if i.system == "clio"]
    trust = next(i for i in fin if "Main Trust Account" in i.summary)
    assert trust.summary.startswith("matter trust account:")
    assert "bal 38446.24" in trust.summary
    # a real zero renders as "bal 0.0", not "? bal"
    operating = next(i for i in fin if "Operating Account" in i.summary)
    assert "bal 0.0" in operating.summary
    # no firm-wide account rows in the dossier
    assert not any(i.summary.startswith("account:") for i in fin)
    assert not hasattr(clio, "bank_fields")  # list_bank_accounts never called
    # "clio:matter_balances" must appear exactly once in sources_reviewed —
    # sl.safe() already records it on a successful pull; a second explicit
    # append (fixed) duplicated the line whenever the matter had ledger rows.
    assert d.sources_reviewed.count("clio:matter_balances") == 1


def test_clio_matter_without_ledger_notes_absence() -> None:
    d = DossierBuilder(clio=FakeClio(matter_balances=[])).build(_cand())
    assert any("no matter-level trust ledger balances" in s for s in d.sources_reviewed)


# -- Estateably internal self-check ------------------------------------------


def _kober_like_estateably() -> FakeEstateably:
    """The client's Kober report: 4 account records ($17,479,211.09) + inventory
    lines ($992,138.42 bank + $1,001,171.08 real estate + two $1 placeholders)
    against a stated current inventory of $18,329,212.09 — a gap presented with
    no flag. No composition of the components reconciles."""
    return FakeEstateably(
        summary=est_item("E1", stage="Administration", currentInventory=18_329_212.09),
        accounts=[
            est_item("A1", accountType="Chequing", meta={"balance": {"value": 10_000_000.00}}),
            est_item("A2", accountType="Savings", meta={"balance": {"value": 5_000_000.00}}),
            est_item("A3", accountType="GIC", meta={"balance": {"value": 2_000_000.00}}),
            est_item("A4", accountType="Investment", meta={"balance": {"value": 479_211.09}}),
        ],
        inventory=[
            est_item("I1", category="Bank Accounts", value=992_138.42),
            est_item("I2", category="Real Estate", value=1_001_171.08),
            est_item("I3", category="Other", value=1.0),
            est_item("I4", category="Other", value=1.0),
        ],
    )


def test_estateably_self_check_flags_component_total_discrepancy() -> None:
    d = DossierBuilder(estateably=_kober_like_estateably()).build(_cand())
    line = next(
        i for i in d.section("financials").items if i.record_id == "estateably-internal"
    )
    assert "DISCREPANCY" in line.summary
    # closest composition = inventory + accounts less the overlapping bank line
    # (19,472,522.59 − 992,138.42 = 18,480,384.17) → gap $151,172.08
    assert "$151,172.08" in line.summary
    assert "stated current inventory $18,329,212.09" in line.summary


def test_estateably_self_check_agrees_when_a_composition_reconciles() -> None:
    fake = FakeEstateably(
        summary=est_item("E1", stage="Admin", currentInventory=100_000.00),
        accounts=[est_item("A1", accountType="Chequing", meta={"balance": {"value": 10_000.0}})],
        inventory=[
            est_item("I1", category="Bank Accounts", value=10_000.0),
            est_item("I2", category="Real Estate", value=90_000.0),
        ],
    )
    d = DossierBuilder(estateably=fake).build(_cand())
    line = next(
        i for i in d.section("financials").items if i.record_id == "estateably-internal"
    )
    assert "agree" in line.summary and "DISCREPANCY" not in line.summary


def test_estateably_self_check_soft_note_when_items_unvalued() -> None:
    # A partially-valued list can't prove a discrepancy — soft note, no flag.
    fake = FakeEstateably(
        summary=est_item("E1", stage="Admin", currentInventory=100_000.00),
        inventory=[
            est_item("I1", category="Real Estate", value="TBD"),
            est_item("I2", category="Other", value=50_000.0),
        ],
    )
    d = DossierBuilder(estateably=fake).build(_cand())
    line = next(
        i for i in d.section("financials").items if i.record_id == "estateably-internal"
    )
    assert "not fully checkable" in line.summary
    assert "DISCREPANCY" not in line.summary


def test_estateably_self_check_silent_without_stated_total() -> None:
    # Default fake summary has no currentInventory → nothing to check against.
    d = DossierBuilder(estateably=FakeEstateably()).build(_cand())
    assert not any(
        i.record_id == "estateably-internal" for i in d.section("financials").items
    )


def test_bounded_pulls_mark_truncation() -> None:
    many = [est_item(f"T{i}", name=f"task {i}", status="open") for i in range(30)]
    limits = DossierLimits(tasks=5)
    d = DossierBuilder(
        estateably=FakeEstateably(tasks=many), clio=FakeClio(tasks=[]), limits=limits
    ).build(_cand())
    est_tasks = [i for i in d.section("tasks").items if i.system == "estateably"]
    # 5 real items + 1 "(+25 more)" marker
    markers = [i for i in est_tasks if i.record_id == "-"]
    assert len(markers) == 1 and "more" in markers[0].summary
    assert len([i for i in est_tasks if i.record_id != "-"]) == 5


def test_partial_failure_is_recorded_not_fatal() -> None:
    d = DossierBuilder(
        estateably=FakeEstateably(tasks=RuntimeError("boom")),
        clio=FakeClio(),
    ).build(_cand())
    # Estateably tasks failed, but the dossier still built and Clio tasks present.
    assert any("estateably:tasks" in e for e in d.errors)
    assert any(i.system == "clio" for i in d.section("tasks").items)
    assert d.section("estate_summary").items  # other estateably pulls still ran


def test_only_available_sources_are_used() -> None:
    # No Clio connector → only Estateably contributes; candidate still builds.
    cand = MatterCandidate(
        name="Smith", refs=[SourceRef("estateably", "E1", "Smith")], matter_type="estate"
    )
    d = DossierBuilder(estateably=FakeEstateably()).build(cand)
    assert {i.system for sec in d.sections.values() for i in sec.items} == {"estateably"}


def test_build_marks_disabled_connectors_distinct_from_candidate_gaps() -> None:
    builder = DossierBuilder(estateably=None, clio=FakeClio(), quickbooks=None, graph=None)
    candidate = MatterCandidate(
        name="Smith Estate",
        refs=[SourceRef("clio", "123", "Smith")],
        matter_type="estate",
        score=1.0,
    )
    dossier = builder.build(candidate)
    # "outlook", not "graph" — matches the vocabulary the synthesis prompt and
    # dossier.errors already use for this source (Important/I3, final review).
    assert dossier.disabled_sources == frozenset({"estateably", "quickbooks", "outlook"})
    assert "clio" not in dossier.disabled_sources


def test_build_with_all_connectors_disabled_does_not_crash() -> None:
    # Review Focus: Matter Search itself is never toggleable, so this
    # combination (every connector off) is reachable and must degrade
    # honestly rather than raise.
    builder = DossierBuilder(estateably=None, clio=None, quickbooks=None, graph=None)
    candidate = MatterCandidate(
        name="Smith Estate",
        refs=[SourceRef("clio", "123", "Smith")],
        matter_type="estate",
        score=1.0,
    )
    dossier = builder.build(candidate)
    assert dossier.disabled_sources == frozenset({"estateably", "clio", "quickbooks", "outlook"})
    assert dossier.errors == []  # nothing was attempted, so nothing failed
    assert dossier.as_dict()["disabled_sources"]


def _est_http_error(status: int, title: str, estate_id: str = "E1") -> HttpError:
    return HttpError(
        f"GET https://x/api/v1/estates/{estate_id}/summary failed: {status}",
        status=status,
        body=json.dumps({"errors": [{"title": title, "status": str(status)}]}),
    )


def _est_items(d) -> list:
    return [i for sec in d.sections.values() for i in sec.items if i.system == "estateably"]


def test_estateably_billing_gate_short_circuits_with_one_clear_note() -> None:
    # Per-case 402 "Missing payment" on the summary canary → one honest note and
    # the ~10 identically-gated sub-resource pulls are skipped (not 10 raw errors).
    fake = FakeEstateably(summary=_est_http_error(402, "Missing payment for case E1"))
    d = DossierBuilder(estateably=fake, clio=FakeClio(tasks=[])).build(_cand())

    est_errors = [e for e in d.errors if e.startswith("estateably")]
    assert len(est_errors) == 1
    assert "billing/payment hold" in est_errors[0] and "402" in est_errors[0]
    assert not _est_items(d)  # gate tripped before any Estateably item was produced
    assert fake.calls == ["summary"]  # short-circuit: only the canary ran
    # One dead source never sinks the dossier — Clio still contributed.
    assert any(i.system == "clio" for sec in d.sections.values() for i in sec.items)


def test_estateably_estate_not_found_short_circuits() -> None:
    fake = FakeEstateably(summary=_est_http_error(404, "Estate not found"))
    d = DossierBuilder(estateably=fake).build(_cand())

    est_errors = [e for e in d.errors if e.startswith("estateably")]
    assert len(est_errors) == 1
    assert "not found" in est_errors[0] and "404" in est_errors[0]
    assert fake.calls == ["summary"]
    assert not _est_items(d)


def test_best_ref_ids_picks_best_matching_ref_per_system() -> None:
    # If a candidate carries >1 ref from a system, the dossier must read the one
    # that best matches the term — not whichever is last (the wrong-case bug).
    cand = MatterCandidate(
        name="David Yurkiw - 190024",
        refs=[
            SourceRef("estateably", "WRONG", "David Jackman - 260020"),
            SourceRef("estateably", "RIGHT", "David Yurkiw - 190024"),
            SourceRef("clio", "C1", "Estate of David Yurkiw"),
        ],
    )
    ids = _best_ref_ids(cand, "David Thomas George Yurkiw")
    assert ids["estateably"] == "RIGHT"  # best token overlap, not last-wins
    assert ids["clio"] == "C1"


def test_matter_title_from_pulled_refs_not_a_merged_foreign_matter() -> None:
    # A small "Bell" result set over-merges a DIFFERENT client's case into the
    # candidate. The status-file title must come from the ref the dossier actually
    # pulls (Desmond), never from the longest label overall — which here belongs to
    # the foreign "Michael Bell Trust" ref and produced the reported wrong title.
    cand = MatterCandidate(
        name="Michael Bell Trust - 240018",  # longest label across ALL refs (the bug source)
        refs=[
            SourceRef("estateably", "DESMOND", "Desmond Bell - 260008"),
            SourceRef("estateably", "MICHAEL", "Michael Bell Trust - 240018"),
        ],
    )
    ref_ids = _best_ref_ids(cand, "desmond bell")
    assert ref_ids["estateably"] == "DESMOND"  # content is Desmond (best overlap)
    # Title must track the pulled ref, not the longer foreign label.
    assert _matter_title(cand, ref_ids) == "Desmond Bell - 260008"


def test_matter_title_keeps_most_descriptive_pulled_label() -> None:
    # Same client across systems: title is the most descriptive (longest) label
    # AMONG THE PULLED refs — the good-case behaviour is preserved.
    cand = MatterCandidate(
        name="Michael Bell Trust - 240018",
        refs=[
            SourceRef("estateably", "DESMOND", "Desmond Bell - 260008"),
            SourceRef("estateably", "MICHAEL", "Michael Bell Trust - 240018"),
            SourceRef("clio", "CLIO", "Estate of Desmond Radloff Bell - Letters of Administration"),
        ],
    )
    ref_ids = _best_ref_ids(cand, "desmond bell")
    title = _matter_title(cand, ref_ids)
    assert title == "Estate of Desmond Radloff Bell - Letters of Administration"
    assert "Michael Bell Trust" not in title


def test_matter_title_falls_back_to_candidate_name() -> None:
    # No refs match the pulled ids (defensive) → fall back to candidate.name.
    cand = MatterCandidate(name="Smith Estate", refs=[SourceRef("clio", "C1", "Smith Estate")])
    assert _matter_title(cand, {}) == "Smith Estate"


def test_build_titles_from_pulled_ref_not_merged_foreign_label() -> None:
    # End-to-end: an over-merged candidate whose longest label is a DIFFERENT
    # client's case ("Michael Bell Trust") must still title the status file with
    # the matter actually pulled (Desmond). Fails pre-fix (title == candidate.name).
    cand = MatterCandidate(
        name="Michael Bell Trust - 240018",
        refs=[
            SourceRef("estateably", "DESMOND", "Desmond Bell - 260008"),
            SourceRef("estateably", "MICHAEL", "Michael Bell Trust - 240018"),
        ],
        matter_type="estate",
    )
    d = DossierBuilder(estateably=FakeEstateably()).build(cand, correspondence_term="desmond bell")
    assert d.matter_name == "Desmond Bell - 260008"


def test_non_gate_summary_error_does_not_short_circuit() -> None:
    # A non-gate 4xx (e.g. 400) must behave as before: recorded, then the rest of
    # the Estateably fan-out still runs and contributes.
    fake = FakeEstateably(summary=_est_http_error(400, "Bad request"))
    d = DossierBuilder(estateably=fake).build(_cand())

    assert any("estateably:summary" in e for e in d.errors)
    assert "documents" in fake.calls  # did NOT short-circuit
    assert _est_items(d)  # other sub-resources still produced items


class FakeGraph:
    def __init__(
        self,
        mailboxes: list[dict[str, Any]],
        by_mailbox: dict[str, list],
        bodies: dict[str, str] | None = None,
        full_bodies: dict[str, str] | None = None,
    ) -> None:
        self._mailboxes = mailboxes
        self._by_mailbox = by_mailbox
        # message id -> uniqueBody text served by get_message hydration; a message
        # id absent from the dict raises (hydration failure path). full_bodies gives
        # a distinct full `body` (incl. quoted/forwarded content) when set — used to
        # test that forwards hydrate from body, replies from uniqueBody.
        self._bodies = bodies
        self._full_bodies = full_bodies or {}
        self.searched: list[tuple[str, str]] = []
        self.search_tops: list[int] = []
        self.hydrated: list[tuple[str, str]] = []
        self.roster_calls = 0

    def list_users(self, *, top: int | None = None) -> list[dict[str, Any]]:
        self.roster_calls += 1
        return self._mailboxes

    def search_mailbox(self, user_id: str, kql: str, *, top: int = 25) -> list[dict[str, Any]]:
        self.searched.append((user_id, kql))
        self.search_tops.append(top)
        value = self._by_mailbox.get(user_id, [])
        if isinstance(value, Exception):
            raise value
        return value

    def get_message(
        self,
        user_id: str,
        message_id: str,
        *,
        select: str | None = None,
        body_as_text: bool = False,
    ) -> dict[str, Any]:
        self.hydrated.append((user_id, message_id))
        if self._bodies is None or message_id not in self._bodies:
            raise RuntimeError(f"GET users/{user_id}/messages/{message_id} failed: 404")
        return {
            "uniqueBody": {"content": self._bodies[message_id]},
            "body": {"content": self._full_bodies.get(message_id, self._bodies[message_id])},
        }


def test_mailbox_roster_is_cached_across_searches() -> None:
    # The org-wide roster is query-independent → fetched once, then reused from the
    # shared cache on subsequent searches (even with a different correspondence term).
    graph = FakeGraph([{"id": "u1", "mail": "a@ht.ca", "accountEnabled": True}], {"u1": []})
    builder = DossierBuilder(graph=graph)
    builder.build(_cand(), correspondence_term="smith")
    builder.build(_cand(), correspondence_term="jones")
    assert graph.roster_calls == 1  # cached; not re-fetched on the second search


class FakeKB:
    """A fake Bedrock Knowledge Base client: returns canned hits or raises."""

    def __init__(self, hits: Any) -> None:
        self._hits = hits
        self.queries: list[str] = []

    def retrieve(self, query: str, *, top_k: int | None = None) -> list[Any]:
        self.queries.append(query)
        if isinstance(self._hits, Exception):
            raise self._hits
        return self._hits


def _msg(
    mid: str,
    subject: str,
    addr: str,
    when: str,
    *,
    to: list[str] | None = None,
    cc: list[str] | None = None,
) -> dict[str, Any]:
    m: dict[str, Any] = {
        "id": mid,
        "subject": subject,
        "from": {"emailAddress": {"address": addr}},
        "receivedDateTime": when,
    }
    if to is not None:
        m["toRecipients"] = [{"emailAddress": {"address": a}} for a in to]
    if cc is not None:
        m["ccRecipients"] = [{"emailAddress": {"address": a}} for a in cc]
    return m


def _lic(id_: str, mail: str) -> dict[str, Any]:
    """A searchable (licensed, enabled) user mailbox fixture."""
    return {"id": id_, "mail": mail, "accountEnabled": True, "assignedLicenses": [{"skuId": "x"}]}


def test_collect_graph_correspondence_across_mailboxes() -> None:
    mailboxes = [
        _lic("u1", "a@ht.ca"),
        _lic("u2", "b@ht.ca"),
        {"id": "u3"},  # no mailbox → skipped
    ]
    graph = FakeGraph(
        mailboxes,
        {
            "u1": [_msg("m1", "Estate of Oonagh McDonald", "nicole@ht.ca", "2026-07-03")],
            "u2": [_msg("m2", "RE: McDonald payment", "ethel@ht.ca", "2026-07-05")],
        },
    )
    d = DossierBuilder(graph=graph).build(_cand(), correspondence_term="oonagh mcdonald")

    corr = d.section("correspondence").items
    assert {i.system for i in corr} == {"outlook"}
    # full-body KQL: bare-term AND (searches subject + body + attachments), one per mailbox
    assert graph.searched[0][1] == "oonagh AND mcdonald"
    assert len(graph.searched) == 2  # only the two real mailboxes
    # most recent first
    assert "RE: McDonald payment" in corr[0].summary  # m2 (2026-07-05) is newest


def test_outlook_full_body_hydrated_into_dossier() -> None:
    # The selected messages' FULL uniqueBody (not the ~255-char preview) reaches
    # the dossier, so the model summarizes actual email substance — the preview
    # ceiling is how a lawsuit-announcing email read as routine.
    msg = {
        **_msg("m1", "McDonald", "a@ht.ca", "2026-07-03"),
        "bodyPreview": "Please find attached…",
    }
    body = (
        "Please find attached the filed notice of civil claim against Aphrodite "
        "Trust obtained from CSO. Have you been served? I propose an application "
        "to strike the claim as an abuse of process."
    )
    graph = FakeGraph([_lic("u1", "a@ht.ca")], {"u1": [msg]}, bodies={"m1": body})
    d = DossierBuilder(graph=graph).build(_cand(), correspondence_term="mcdonald")
    item = d.section("correspondence").items[0]
    assert graph.hydrated == [("u1", "m1")]
    assert "notice of civil claim" in item.data["body"]
    assert "abuse of process" in item.data["body"]
    assert item.summary.startswith("McDonald — from")  # header line stays compact


def test_outlook_hydration_failure_degrades_to_preview() -> None:
    # A failed body fetch must not sink the slice — the item falls back to the
    # search hit's bodyPreview and the shortfall is reported.
    msg = {
        **_msg("m1", "McDonald", "a@ht.ca", "2026-07-03"),
        "bodyPreview": "Please find the signed beneficiary release attached.",
    }
    graph = FakeGraph([_lic("u1", "a@ht.ca")], {"u1": [msg]})  # no bodies → 404
    d = DossierBuilder(graph=graph).build(_cand(), correspondence_term="mcdonald")
    item = d.section("correspondence").items[0]
    assert item.data["body"] == "Please find the signed beneficiary release attached."
    assert any("1 message bodies unavailable" in r for r in d.sources_reviewed)
    assert not d.errors


def test_outlook_long_body_truncated_with_marker() -> None:
    # Truncation is explicit, never silent — and cuts at a word boundary.
    msg = _msg("m1", "McDonald", "a@ht.ca", "2026-07-03")
    graph = FakeGraph(
        [_lic("u1", "a@ht.ca")], {"u1": [msg]}, bodies={"m1": "word " * 1000}
    )
    d = DossierBuilder(graph=graph).build(_cand(), correspondence_term="mcdonald")
    body = d.section("correspondence").items[0].data["body"]
    assert body.endswith("…[truncated — open in Outlook]")
    assert len(body) <= 1500 + len(" …[truncated — open in Outlook]")


def test_outlook_dedups_cross_mailbox_copies() -> None:
    # The same message lands once per recipient mailbox; only ONE copy may enter
    # the dossier (duplicates were crowding out unique threads), with the copy
    # count recorded and reported.
    def copy(mid: str) -> dict[str, Any]:
        return {
            **_msg(mid, "RE: McDonald", "nicole@ht.ca", "2026-07-05T20:00:00Z"),
            "internetMessageId": "<thread-1@ht.ca>",
        }

    solo = {
        **_msg("m9", "McDonald instructions", "nicole@ht.ca", "2026-07-01T10:00:00Z"),
        "internetMessageId": "<solo@ht.ca>",
    }
    graph = FakeGraph(
        [_lic("u1", "a@ht.ca"), _lic("u2", "b@ht.ca"), _lic("u3", "c@ht.ca")],
        {"u1": [copy("m1"), solo], "u2": [copy("m2")], "u3": [copy("m3")]},
        bodies={},
    )
    d = DossierBuilder(graph=graph).build(_cand(), correspondence_term="mcdonald")
    corr = [i for i in d.section("correspondence").items if i.record_id != "-"]
    assert len(corr) == 2  # one copy of the thread message + the solo message
    thread_item = next(i for i in corr if "RE: McDonald" in i.summary)
    assert thread_item.data.get("also_in") == 2  # two other mailboxes held copies
    assert any("2 duplicate copies removed" in r for r in d.sources_reviewed)


def test_is_excluded_correspondent() -> None:
    # A @loop8.io participant on from / to / cc (or a subdomain) → excluded;
    # a normal HT↔external email → kept; a DN sender with no "@" is safe.
    assert orch._is_excluded_correspondent(_msg("m", "s", "dev@loop8.io", "2026-07-01"))
    assert orch._is_excluded_correspondent(
        _msg("m", "s", "nicole@ht.ca", "2026-07-01", to=["dev@loop8.io"])
    )
    assert orch._is_excluded_correspondent(
        _msg("m", "s", "nicole@ht.ca", "2026-07-01", cc=["dev@mail.loop8.io"])
    )
    assert not orch._is_excluded_correspondent(
        _msg("m", "s", "nicole@ht.ca", "2026-07-01", to=["counsel@scionlaw.ca"])
    )
    assert not orch._is_excluded_correspondent(  # "loop8.io" only as a substring, not the domain
        _msg("m", "s", "person@notloop8.iox", "2026-07-01")
    )
    assert not orch._is_excluded_correspondent(
        {"from": {"emailAddress": {"address": "/O=EXCHANGELABS/OU=.../CN=X"}}}
    )


def test_outlook_excludes_loop8_dev_correspondence() -> None:
    # Emails exchanged with the dev team (@loop8.io) — as sender OR recipient —
    # are POC noise and must not enter the dossier; the count is reported.
    graph = FakeGraph(
        [_lic("u1", "a@ht.ca")],
        {
            "u1": [
                _msg("real", "McDonald probate update", "counsel@scionlaw.ca", "2026-07-05"),
                _msg("dev-from", "New AI tool is live", "team@loop8.io", "2026-07-06"),
                _msg(
                    "dev-to", "RE: McDonald test run", "nicole@ht.ca", "2026-07-07",
                    to=["dev@loop8.io"],
                ),
            ]
        },
        bodies={},
    )
    d = DossierBuilder(graph=graph).build(_cand(), correspondence_term="mcdonald")
    subjects = " || ".join(i.summary for i in d.section("correspondence").items)
    assert "McDonald probate update" in subjects  # the real client email survived
    assert "New AI tool is live" not in subjects  # dev-from excluded
    assert "McDonald test run" not in subjects  # dev-to excluded
    assert any("2 Loop8 dev/test email(s) excluded" in r for r in d.sources_reviewed)


def test_outlook_conversation_cap_preserves_unique_threads() -> None:
    # A busy thread may not evict single-message threads: with a cap of 8 per
    # conversation, the older solo instructions email still makes the cut even
    # when a newer mega-thread has more messages than the correspondence limit.
    thread = [
        {
            **_msg(f"t{i}", f"RE: McDonald {i}", "x@ht.ca", f"2026-07-2{i % 10}T0{i % 10}:00:00Z"),
            "internetMessageId": f"<t{i}@ht.ca>",
            "conversationId": "conv-big",
        }
        for i in range(12)
    ]
    solo = {
        **_msg("solo", "McDonald instructions", "nicole@ht.ca", "2026-07-01T10:00:00Z"),
        "internetMessageId": "<solo@ht.ca>",
        "conversationId": "conv-solo",
    }
    graph = FakeGraph(
        [_lic("u1", "a@ht.ca")],
        {"u1": [*thread, solo]},
        bodies={},
    )
    limits = DossierLimits(correspondence=10)
    d = DossierBuilder(graph=graph, limits=limits).build(_cand(), correspondence_term="mcdonald")
    corr = [i for i in d.section("correspondence").items if i.record_id != "-"]
    assert len(corr) == 10
    assert any("McDonald instructions" in i.summary for i in corr)  # the solo thread survived
    big = [i for i in corr if "RE: McDonald" in i.summary]  # the busy conversation
    assert len(big) == 9  # 8 under the cap + 1 backfilled into the free slot


def test_outlook_sender_prefers_display_name_over_exchange_dn() -> None:
    # Internal senders carry a legacy X.500 DN in `address` — render the display
    # name (or a placeholder), never the DN.
    dn = "/O=EXCHANGELABS/OU=EXCHANGE ADMINISTRATIVE GROUP/CN=RECIPIENTS/CN=ABC123"
    named = {
        "id": "m1",
        "subject": "McDonald",
        "from": {"emailAddress": {"address": dn, "name": "Ethel Smith"}},
        "receivedDateTime": "2026-07-03T10:00:00Z",
    }
    anon = {
        "id": "m2",
        "subject": "RE: McDonald",
        "from": {"emailAddress": {"address": dn}},
        "receivedDateTime": "2026-07-04T10:00:00Z",
    }
    graph = FakeGraph([_lic("u1", "a@ht.ca")], {"u1": [named, anon]}, bodies={})
    d = DossierBuilder(graph=graph).build(_cand(), correspondence_term="mcdonald")
    summaries = [i.summary for i in d.section("correspondence").items]
    assert any("from Ethel Smith" in s for s in summaries)  # named sender rendered
    assert not any("EXCHANGELABS" in s for s in summaries)  # legacy DN never rendered
    assert any("from (internal sender)" in s for s in summaries)  # anon DN → placeholder


def test_outlook_dates_render_in_pacific_time() -> None:
    # Graph returns UTC; a 9:38 pm Pacific email must not shift to the next day.
    msg = _msg("m1", "McDonald", "a@ht.ca", "2026-07-30T04:38:00Z")  # = 07-29 21:38 PDT
    graph = FakeGraph([_lic("u1", "a@ht.ca")], {"u1": [msg]}, bodies={})
    d = DossierBuilder(graph=graph).build(_cand(), correspondence_term="mcdonald")
    item = d.section("correspondence").items[0]
    assert item.date == "2026-07-29T21:38:00-07:00"
    assert any(
        "reviewed to 2026-07-29 9:38 pm Pacific" in r for r in d.sources_reviewed
    )


def test_outlook_reviewed_to_note_reports_run_cutoff() -> None:
    graph = FakeGraph(
        [_lic("u1", "a@ht.ca"), _lic("u2", "b@ht.ca")],
        {
            "u1": [_msg("m1", "McDonald", "a@ht.ca", "2026-07-03T10:00:00Z")],
            "u2": [_msg("m2", "RE: McDonald", "b@ht.ca", "2026-07-05T22:00:00Z")],
        },
        bodies={},
    )
    d = DossierBuilder(graph=graph).build(_cand(), correspondence_term="mcdonald")
    cutoff = next(r for r in d.sources_reviewed if r.startswith("Outlook correspondence"))
    assert "reviewed to 2026-07-05 3:00 pm Pacific" in cutoff  # 22:00Z = 15:00 PDT
    assert "2 unique messages across 2 mailboxes searched" in cutoff


def test_outlook_search_fetches_raised_per_mailbox_page() -> None:
    # Matter-heavy mailboxes rank on-matter threads past the old top=20 cut (Graph
    # relevance, not recency, no pagination), silently dropping them. The per-mailbox
    # page must be fetched at the raised cap so those threads are recovered.
    graph = FakeGraph([_lic("u1", "a@ht.ca")], {"u1": []})
    DossierBuilder(graph=graph).build(_cand(), correspondence_term="mcdonald")
    assert graph.search_tops == [50]


def test_outlook_forward_hydrates_from_full_body() -> None:
    # A FORWARD's substance is the forwarded/quoted content, which uniqueBody strips.
    # It must hydrate from the full body, or a forwarded email announcing (e.g.)
    # property issues reaches the model as only the sender's one-line cover note.
    fwd = _msg("f1", "FW: Estate of Lovro Gusak", "apearl@cwilson.com", "2026-07-29T22:00:00Z")
    cover = "Hi Nada, below is Karol's email on the property."
    graph = FakeGraph(
        [_lic("u1", "a@ht.ca")],
        {"u1": [fwd]},
        bodies={"f1": cover},  # uniqueBody = cover note only
        full_bodies={
            "f1": cover + " From: Karol Sent: ... The secondary gate is unsecured; "
            "retain a local caretaker for maintenance and a local realtor pending the "
            "partition proceedings."
        },
    )
    d = DossierBuilder(graph=graph).build(_cand(), correspondence_term="gusak")
    body = next(
        i for i in d.section("correspondence").items if "FW: Estate of Lovro Gusak" in i.summary
    ).data["body"]
    assert "gate is unsecured" in body
    assert "realtor" in body and "partition" in body


def test_outlook_reply_hydrates_from_uniquebody_not_quoted_history() -> None:
    # A reply keeps uniqueBody (its new text only) so the quoted chain isn't re-included.
    reply = _msg("r1", "RE: Estate of Lovro Gusak", "x@ht.ca", "2026-07-29T22:00:00Z")
    graph = FakeGraph(
        [_lic("u1", "a@ht.ca")],
        {"u1": [reply]},
        bodies={"r1": "My new reply text."},
        full_bodies={"r1": "My new reply text. From: old thread QUOTED-HISTORY-NOT-WANTED."},
    )
    d = DossierBuilder(graph=graph).build(_cand(), correspondence_term="gusak")
    body = next(
        i for i in d.section("correspondence").items if "RE: Estate of Lovro Gusak" in i.summary
    ).data["body"]
    assert "new reply text" in body
    assert "QUOTED-HISTORY-NOT-WANTED" not in body


def test_outlook_reviewed_note_reports_true_reached_count_when_capped() -> None:
    # Honesty: when the correspondence cap trims the pool the cutoff line must report
    # what actually reached the summary ("{kept} of {found} … oldest N omitted"), not
    # the pre-cap total as if all of it were synthesized.
    msgs = [
        _msg(f"m{i}", f"McDonald {i}", "a@ht.ca", f"2026-07-0{i}T10:00:00Z") for i in range(1, 5)
    ]
    graph = FakeGraph([_lic("u1", "a@ht.ca")], {"u1": msgs}, bodies={})
    d = DossierBuilder(graph=graph, limits=DossierLimits(correspondence=2)).build(
        _cand(), correspondence_term="mcdonald"
    )
    cutoff = next(r for r in d.sources_reviewed if r.startswith("Outlook correspondence"))
    assert "2 of 4 unique messages included (oldest 2 omitted for length)" in cutoff
    # the model still gets an explicit placeholder for the trimmed tail
    assert any(
        i.record_id == "-" and "(+2 more)" in i.summary
        for i in d.section("correspondence").items
    )


def test_outlook_no_hits_note() -> None:
    graph = FakeGraph([_lic("u1", "a@ht.ca")], {"u1": []})
    d = DossierBuilder(graph=graph).build(_cand(), correspondence_term="mcdonald")
    assert any(
        "searched across 1 mailboxes — no matching messages found" in r
        for r in d.sources_reviewed
    )


def test_outlook_items_link_to_the_message_weblink() -> None:
    msg = {
        **_msg("m1", "McDonald", "a@ht.ca", "2026-07-03"),
        "webLink": "https://outlook.office.com/mail/m1",
    }
    graph = FakeGraph([_lic("u1", "a@ht.ca")], {"u1": [msg]})
    d = DossierBuilder(graph=graph).build(_cand(), correspondence_term="mcdonald")
    item = d.section("correspondence").items[0]
    assert item.data.get("url") == "https://outlook.office.com/mail/m1"


def test_collect_graph_surfaces_mailbox_cap_overflow() -> None:
    # More searchable mailboxes than the cap → overflow is reported, not silent.
    boxes = [_lic(f"u{i}", f"u{i}@ht.ca") for i in range(5)]
    graph = FakeGraph(boxes, {b["id"]: [] for b in boxes})
    limits = DossierLimits(mailboxes=3)
    d = DossierBuilder(graph=graph, limits=limits).build(_cand(), correspondence_term="mcdonald")

    assert len(graph.searched) == 3  # only up to the cap searched
    assert any("2 over the 3 cap not searched" in r for r in d.sources_reviewed)


def test_collect_graph_includes_unlicensed_but_skips_disabled_and_guest() -> None:
    mailboxes = [
        _lic("u1", "a@ht.ca"),
        {"id": "u2", "mail": "shared@ht.ca"},  # unlicensed shared inbox → still searched
        {"id": "u3", "mail": "gone@ht.ca", "accountEnabled": False},  # disabled → skip
        {"id": "u4", "mail": "guest@x.com", "userType": "Guest"},  # guest → skip
        {"id": "u5"},  # no mailbox address → skip
    ]
    graph = FakeGraph(
        mailboxes,
        {
            "u1": [_msg("m1", "McDonald", "a@ht.ca", "2026-07-01")],
            "u2": [_msg("m2", "RE: McDonald", "shared@ht.ca", "2026-07-02")],
        },
    )
    d = DossierBuilder(graph=graph).build(_cand(), correspondence_term="mcdonald")

    # the unlicensed shared mailbox IS searched; disabled/guest/no-mail are not
    assert sorted(s[0] for s in graph.searched) == ["u1", "u2"]
    reviewed = " ".join(d.sources_reviewed)
    assert "3 disabled/guest/no-mail skipped" in reviewed
    assert not d.errors


def test_collect_graph_no_mailbox_is_quiet_skip_but_real_error_flagged() -> None:
    mailboxes = [
        _lic("u1", "a@ht.ca"),
        _lic("u2", "admin@ht.ca"),  # enabled but no REST mailbox → 404
        _lic("u3", "flaky@ht.ca"),  # transient server error → real failure
    ]
    graph = FakeGraph(
        mailboxes,
        {
            "u1": [_msg("m1", "McDonald", "a@ht.ca", "2026-07-01")],
            "u2": RuntimeError("GET users/u2/messages failed: 404"),
            "u3": RuntimeError("GET users/u3/messages failed: 503"),
        },
    )
    d = DossierBuilder(graph=graph).build(_cand(), correspondence_term="mcdonald")

    reviewed = " ".join(d.sources_reviewed)
    assert "1 without an accessible mailbox" in reviewed  # the 404 is a quiet skip
    assert d.errors == ["outlook: 1 mailbox searches failed"]  # the 503 is flagged


def test_collect_graph_skipped_without_term() -> None:
    graph = FakeGraph(
        [{"id": "u1", "mail": "a@ht.ca"}],
        {"u1": [_msg("m1", "x", "y@ht.ca", "2026-01-01")]},
    )
    d = DossierBuilder(graph=graph).build(_cand())  # no correspondence_term
    assert "correspondence" not in d.sections


# -- SharePoint documents (Bedrock Knowledge Base) --------------------------


def _kbhit(text: str, name: str, *, web_url: str | None = None) -> KBResult:
    md: dict[str, Any] = {"name": name, "last_modified": "2026-07-01"}
    if web_url is not None:
        md["web_url"] = web_url
    return KBResult(text=text, score=0.8, uri=f"s3://aphrodite-crawl/{name}", metadata=md)


def test_collect_sharepoint_retrieves_documents_from_kb() -> None:
    # A two-token client scope ({"john", "smith"}) so on-matter hits clear the
    # confidentiality filter — these tests exercise retrieval, not the scoping rule.
    cand = MatterCandidate(
        name="Estate of John Smith",
        refs=[SourceRef("clio", "123", "Smith", qb_customer_id="CID9")],
        matter_type="estate",
    )
    kb = FakeKB(
        [
            _kbhit(
                "Grant of probate issued for the estate of John Smith on 2024-03-01.",
                "probate.pdf",
                web_url="https://x/sites/DataA/Matters/Smith/probate.pdf",
            ),
            _kbhit("Deed of transfer for the John Smith property.", "deed.pdf"),
        ]
    )
    d = DossierBuilder(knowledge_base=kb).build(cand, correspondence_term="smith")

    docs = d.section("documents").items
    assert {i.system for i in docs} == {"sharepoint"}
    # record_id is opaque (not the filename), so a filename with spaces/parens can't
    # leak into a citation marker; the human filename stays in the summary.
    assert all(i.record_id.startswith("sp-") for i in docs)
    summaries = " ".join(i.summary for i in docs)
    assert "probate.pdf" in summaries and "deed.pdf" in summaries
    probate = next(i for i in docs if "probate.pdf" in i.summary)
    assert "Grant of probate" in probate.summary  # body text, not just the filename
    assert probate.data.get("url") == "https://x/sites/DataA/Matters/Smith/probate.pdf"
    assert kb.queries == ["smith"]
    assert "2 document excerpt(s) retrieved" in " ".join(d.sources_reviewed)
    assert not d.errors


def test_collect_sharepoint_excludes_hr_finance_defensively() -> None:
    cand = MatterCandidate(
        name="Estate of John Smith",
        refs=[SourceRef("clio", "123", "Smith")],
        matter_type="estate",
    )
    kb = FakeKB(
        [
            _kbhit(
                "John Smith matter doc.",
                "will.pdf",
                web_url="https://x/sites/DataA/Matters/Smith/will.pdf",
            ),
            _kbhit("Payroll.", "pay.xlsx", web_url="https://x/sites/DataA/Finance/2026/pay.xlsx"),
        ]
    )
    d = DossierBuilder(knowledge_base=kb).build(cand, correspondence_term="smith")

    docs = d.section("documents").items
    assert len(docs) == 1 and "will.pdf" in docs[0].summary  # the Finance doc is dropped
    assert "1 HR/Finance excluded" in " ".join(d.sources_reviewed)


def test_dossier_builder_uses_custom_excluded_folders() -> None:
    # "payroll" isn't in the default HR/Finance exclusion set — this proves the
    # override, not just the default, is what's doing the excluding.
    cand = MatterCandidate(
        name="Estate of John Smith",
        refs=[SourceRef("clio", "123", "Smith")],
        matter_type="estate",
    )
    kb = FakeKB(
        [
            _kbhit(
                "John Smith matter doc.",
                "will.pdf",
                web_url="https://x/sites/DataA/Matters/Smith/will.pdf",
            ),
            _kbhit("Payroll.", "pay.xlsx", web_url="https://x/sites/DataA/Payroll/2026/pay.xlsx"),
        ]
    )
    d = DossierBuilder(
        knowledge_base=kb, excluded_sharepoint_folders=frozenset({"payroll"})
    ).build(cand, correspondence_term="smith")

    docs = d.section("documents").items
    assert len(docs) == 1 and "will.pdf" in docs[0].summary  # the Payroll doc is dropped


def _payroll_and_finance_kb() -> FakeKB:
    return FakeKB(
        [
            _kbhit(
                "John Smith matter doc.",
                "will.pdf",
                web_url="https://x/sites/DataA/Matters/Smith/will.pdf",
            ),
            # On-matter text, so only the folder exclusion (not the matter-scope
            # filter) decides whether these two survive.
            _kbhit(
                "John Smith payroll.",
                "pay.xlsx",
                web_url="https://x/sites/DataA/Payroll/2026/pay.xlsx",
            ),
            _kbhit(
                "John Smith ledger.",
                "gl.xlsx",
                web_url="https://x/sites/DataA/Finance/2026/gl.xlsx",
            ),
        ]
    )


def _smith_cand() -> MatterCandidate:
    return MatterCandidate(
        name="Estate of John Smith",
        refs=[SourceRef("clio", "123", "Smith")],
        matter_type="estate",
    )


def test_build_override_replaces_the_constructed_exclusion_set() -> None:
    # Constructed with {"payroll"}, overridden per call with {"finance"}: the
    # override wins outright (Finance dropped, Payroll kept) — not unioned.
    builder = DossierBuilder(
        knowledge_base=_payroll_and_finance_kb(),
        excluded_sharepoint_folders=frozenset({"payroll"}),
    )
    d = builder.build(
        _smith_cand(),
        correspondence_term="smith",
        excluded_sharepoint_folders=frozenset({"finance"}),
    )
    docs = d.section("documents").items
    summaries = " ".join(i.summary for i in docs)
    assert len(docs) == 2
    assert "pay.xlsx" in summaries and "will.pdf" in summaries
    assert "gl.xlsx" not in summaries


def test_build_override_empty_set_excludes_nothing() -> None:
    # An explicit empty override is honored ("exclude nothing"), not treated as unset.
    builder = DossierBuilder(
        knowledge_base=_payroll_and_finance_kb(),
        excluded_sharepoint_folders=frozenset({"payroll", "finance"}),
    )
    d = builder.build(
        _smith_cand(), correspondence_term="smith", excluded_sharepoint_folders=frozenset()
    )
    assert len(d.section("documents").items) == 3


def test_build_without_override_uses_the_constructed_exclusion_set() -> None:
    builder = DossierBuilder(
        knowledge_base=_payroll_and_finance_kb(),
        excluded_sharepoint_folders=frozenset({"payroll"}),
    )
    d = builder.build(_smith_cand(), correspondence_term="smith")
    summaries = " ".join(i.summary for i in d.section("documents").items)
    assert "pay.xlsx" not in summaries and "gl.xlsx" in summaries


def test_collect_sharepoint_excludes_off_matter_documents() -> None:
    # The firm-wide index returns another client's document; it must be dropped
    # (confidentiality) — it carries neither this matter's number nor client name.
    cand = MatterCandidate(
        name="Estate of Vista Trethewey - 250028",
        refs=[SourceRef("clio", "C1", "Trethewey")],
        matter_type="estate",
    )
    kb = FakeKB(
        [
            _kbhit(
                "Will of Vista Trethewey.",
                "Will - Trethewey.pdf",
                web_url="https://x/sites/ExampleSite/_layouts/15/Doc.aspx?file=Will.DOC",
            ),
            _kbhit(
                "Trust deed for the Jones family.",
                "Jones-deed.pdf",
                web_url="https://x/sites/ExampleSite/Shared%20Documents/C/9999/240099/DOC/Jones.pdf",
            ),
        ]
    )
    d = DossierBuilder(knowledge_base=kb).build(cand, correspondence_term="Trethewey")

    docs = d.section("documents").items
    assert len(docs) == 1 and "Will - Trethewey.pdf" in docs[0].summary  # Jones dropped
    assert "1 off-matter excluded" in " ".join(d.sources_reviewed)


def test_collect_sharepoint_keeps_generic_doc_via_matter_number_in_path() -> None:
    # A generically-named document is kept when its folder path carries the matter
    # number (path-style URL), even without the client name in the filename.
    cand = MatterCandidate(
        name="Estate of Vista Trethewey - 250028",
        refs=[SourceRef("clio", "C1", "Trethewey")],
        matter_type="estate",
    )
    kb = FakeKB(
        [
            _kbhit(
                "Client questionnaire.",
                "questionnaire.pdf",
                web_url="https://x/sites/ExampleSite/Shared%20Documents/C/2485/250028/DOC/q.pdf",
            ),
        ]
    )
    d = DossierBuilder(knowledge_base=kb).build(cand, correspondence_term="Trethewey")
    docs = d.section("documents").items
    assert len(docs) == 1 and "questionnaire.pdf" in docs[0].summary


def test_sharepoint_skipped_when_no_scope_keys_derivable() -> None:
    # Fail closed: a matter yielding neither a 6-digit number nor a >=4-char name
    # token must not run an unscoped firm-wide retrieval — the pull is skipped and
    # the skip is reported deterministically.
    kb = FakeKB([_kbhit("Anything.", "doc.pdf")])
    cand = MatterCandidate(name="Ng", refs=[SourceRef("clio", "C1", "Ng")], matter_type=None)
    d = DossierBuilder(knowledge_base=kb).build(cand, correspondence_term="ng")
    assert d.section("documents").items == []
    assert kb.queries == []  # the KB was never queried
    assert any("SharePoint: skipped — no matter number" in s for s in d.sources_reviewed)


def test_collect_sharepoint_soft_handles_kb_failure() -> None:
    kb = FakeKB(RuntimeError("Bedrock retrieve failed: throttled"))
    d = DossierBuilder(knowledge_base=kb).build(_cand(), correspondence_term="mcdonald")

    assert "documents" not in d.sections
    assert any(e.startswith("sharepoint:") for e in d.errors)  # recorded, not fatal


def test_sharepoint_skipped_when_no_kb_configured() -> None:
    d = DossierBuilder().build(_cand(), correspondence_term="mcdonald")
    assert "documents" not in d.sections


def test_as_dict_is_serializable_and_shaped() -> None:
    d = DossierBuilder(estateably=FakeEstateably(), clio=FakeClio()).build(_cand())
    out = d.as_dict()
    assert out["matter_name"] == "Estate of Smith"  # title from the pulled estateably ref
    assert "tasks" in out["sections"]
    item = out["sections"]["tasks"]["items"][0]
    assert {"system", "record_id", "date", "summary"} <= set(item)


# -- Estateably financials (accounts / inventory / summary) -----------------


def test_estateably_account_shows_balance_type_and_masked_number() -> None:
    d = DossierBuilder(estateably=FakeEstateably(), clio=FakeClio(tasks=[])).build(_cand())
    acct = next(i for i in d.section("financials").items if i.record_id == "A1")
    s = acct.summary
    assert "Estate Bank Account" in s and "RBC" in s  # labeled by type + institution
    assert "12500.5" in s  # real balance, read from meta.balance.value (was empty before)
    assert "1234567890" not in s and "***7890" in s  # account number masked


def test_estateably_inventory_labels_category_type_and_capital() -> None:
    d = DossierBuilder(estateably=FakeEstateably(), clio=FakeClio(tasks=[])).build(_cand())
    inv = next(i for i in d.section("financials").items if i.record_id == "I1")
    s = inv.summary
    assert "GIC" in s and "Asset" in s and "50000" in s and "Capital" in s


def test_estate_summary_surfaces_named_figures() -> None:
    fake = FakeEstateably(
        summary=est_item("E1", stage="Administration", estateValue=800000, residual=250000)
    )
    d = DossierBuilder(estateably=fake, clio=FakeClio(tasks=[])).build(_cand())
    summ = next(
        i for i in d.section("estate_summary").items if i.record_id == "E1" and "stage" in i.summary
    )
    assert "estate value: 800000" in summ.summary
    assert "residual: 250000" in summ.summary


# -- PII masking + opaque SharePoint ids ------------------------------------


def test_redact_pii_masks_sin_and_account_numbers() -> None:
    data = {
        "sinNumber": "123456789",
        "accountNumber": "1234567890",
        "name": "Jane Doe",
        "nested": {"sin": "987654321", "note": "ok"},
    }
    out = orch._redact_pii(data)
    assert out["sinNumber"] == "***"
    assert out["accountNumber"] == "***7890"  # last 4 only
    assert out["name"] == "Jane Doe"  # non-sensitive untouched
    assert out["nested"]["sin"] == "***" and out["nested"]["note"] == "ok"


def test_mask_free_text_rules() -> None:
    cases = [
        # long bare digit runs mask to last 4 (the original account-run rule)
        ("CIBC Trust 1234567890", "CIBC Trust ***7890"),
        ("RBC GIC 009602098440010", "RBC GIC ***0010"),
        # 6-digit + short letter suffix = account/policy shape ("851620-T")
        ("TD RRIF 851620-T statement", "TD RRIF ***1620-T statement"),
        # letter-prefixed, grouped contract number ("F416, 280-6" — Sun Life RRIF)
        ("RRIF Contract No. F416, 280-6", "RRIF Contract No. ***2806"),
        ("contract F416280-6 done", "contract ***2806 done"),
        # separated digit groups totalling >=9 digits mask ("00960209844-0010")
        ("deposit 00960209844-0010", "deposit ***0010"),
        ("call 604-716-2668", "call ***2668"),  # phone numbers are PII too
        # --- must SURVIVE ---
        ("Matter 190024", "Matter 190024"),  # 6-digit matter number
        ("invoice 10776 paid", "invoice 10776 paid"),  # 5-digit invoice number
        ("P261759 HANCOCK - CHECKLIST", "P261759 HANCOCK - CHECKLIST"),  # file ref
        ("Action No. S224727 filed", "Action No. S224727 filed"),  # court file ref
        ("2024 - T4A (OAS)", "2024 - T4A (OAS)"),  # tax slip label
        ("sign AUT-01 and return", "sign AUT-01 and return"),  # form code
        ("United Revenue 250005-01 Director", "United Revenue 250005-01 Director"),  # matter sub#
        ("WESA s. 132 order", "WESA s. 132 order"),  # statute section
        ("filed 2026-07-13", "filed 2026-07-13"),  # ISO date
        ("reviewed 2026-07-13 15", "reviewed 2026-07-13 15"),  # datetime-ish prefix
        ("inventory 18329212.09 total", "inventory 18329212.09 total"),  # money
        ("total $19,472,522.59", "total $19,472,522.59"),  # comma-grouped money
        ("balance 19 472 522.59", "balance 19 472 522.59"),  # spaced money w/ cents
        ("NOA _10_241.90.pdf", "NOA _10_241.90.pdf"),  # filename amount convention
        ("5881 85 short groups", "5881 85 short groups"),  # < 9 digits total
    ]
    for raw, expected in cases:
        assert orch._mask_free_text(raw) == expected, raw


def test_mask_free_text_applied_to_sharepoint_and_notes() -> None:
    # Doc titles and note text are free-text PII surfaces — account numbers in
    # them must be masked in the dossier (client saw "851620-T" leak via a title).
    kb = FakeKB(
        [
            _kbhit(
                "Registered fund statement.",
                "TD RRIF 851620-T 190001.pdf",
                web_url="https://x/sites/x/Shared%20Documents/C/1/190001/DOC/td.pdf",
            )
        ]
    )
    cand = MatterCandidate(
        name="Estate - 190001", refs=[SourceRef("clio", "C1", "X")], matter_type="estate"
    )
    d = DossierBuilder(knowledge_base=kb).build(cand, correspondence_term="190001")
    item = d.section("documents").items[0]
    assert "851620-T" not in item.summary
    assert "***1620-T" in item.summary
    assert "190001" in item.summary  # matter number survives


def test_estateably_attributes_are_pii_masked_in_dossier() -> None:
    fake = FakeEstateably(
        summary=est_item("E1", stage="Admin", sinNumber="123456789", accountNumber="9876543210")
    )
    d = DossierBuilder(estateably=fake, clio=FakeClio(tasks=[])).build(_cand())
    item = next(i for i in d.section("estate_summary").items if "sinNumber" in i.data)
    assert item.data["sinNumber"] == "***"
    assert item.data["accountNumber"] == "***3210"


def test_estateably_document_title_is_masked() -> None:
    # Account numbers arriving via an Estateably document TITLE must be masked too
    # (the TD RRIF "851620-T" leak came through a document title, not a structured
    # account field). The matter number in the title still survives.
    fake = FakeEstateably(documents=[est_item("D1", name="TD RRIF #851620-T 250005.pdf")])
    d = DossierBuilder(estateably=fake, clio=FakeClio(tasks=[])).build(_cand())
    titles = " ".join(i.summary for i in d.section("kyc").items if "doc:" in i.summary)
    assert "851620-T" not in titles
    assert "***1620-T" in titles
    assert "250005" in titles  # matter number survives


def test_terminal_t1_deadline_computed_from_date_of_death() -> None:
    # Death Jan-Oct -> April 30 of the following year, computed in code (not by the
    # LLM, which produced wrong dates). Kober 2025-01-25 -> 2026-04-30.
    fake = FakeEstateably(intake=est_item("E1", status="x", dateOfDeath="2025-01-25"))
    d = DossierBuilder(estateably=fake, clio=FakeClio(tasks=[])).build(_cand())
    item = next(i for i in d.section("deadlines").items if i.record_id == "terminal-t1")
    assert "2026-04-30" in item.summary
    assert item.system == "computed"


def test_terminal_t1_deadline_bell_case() -> None:
    # The regression case: 2026-01-15 death -> 2027-04-30 (the LLM had said "July 15, 2027").
    fake = FakeEstateably(intake=est_item("E1", status="x", dateOfDeath="2026-01-15"))
    d = DossierBuilder(estateably=fake, clio=FakeClio(tasks=[])).build(_cand())
    item = next(i for i in d.section("deadlines").items if i.record_id == "terminal-t1")
    assert "2027-04-30" in item.summary
    assert "2027-07-15" not in item.summary


def test_terminal_t1_deadline_nov_dec_death_is_six_months() -> None:
    # Nov/Dec deaths: due 6 months after death (day clamped). Dec 31 2025 -> Jun 30 2026.
    fake = FakeEstateably(intake=est_item("E1", status="x", dateOfDeath="2025-12-31"))
    d = DossierBuilder(estateably=fake, clio=FakeClio(tasks=[])).build(_cand())
    item = next(i for i in d.section("deadlines").items if i.record_id == "terminal-t1")
    assert "2026-06-30" in item.summary


def test_terminal_t1_deadline_flags_estateably_mismatch() -> None:
    # If Estateably records a different terminal-T1 deadline, flag the discrepancy.
    fake = FakeEstateably(
        intake=est_item(
            "E1", status="x", dateOfDeath="2025-01-25", terminalReturnFilingDeadline="2026-06-15"
        )
    )
    d = DossierBuilder(estateably=fake, clio=FakeClio(tasks=[])).build(_cand())
    item = next(i for i in d.section("deadlines").items if i.record_id == "terminal-t1")
    assert "2026-04-30" in item.summary  # computed date
    assert "DISCREPANCY" in item.summary and "2026-06-15" in item.summary


def test_terminal_t1_deadline_absent_without_date_of_death() -> None:
    # No date of death -> no computed line (model falls back to its own handling).
    fake = FakeEstateably(intake=est_item("E1", status="x"))
    d = DossierBuilder(estateably=fake, clio=FakeClio(tasks=[])).build(_cand())
    assert not any(i.record_id == "terminal-t1" for i in d.section("deadlines").items)


def test_sharepoint_record_id_is_opaque_single_token() -> None:
    # A filename with spaces/parens must not become the record_id (it would break
    # the single-token "(system:record_id)" citation marker and leak fragments).
    kb = FakeKB(
        [
            _kbhit(
                "Investment account statement.",
                "MD Mgt account (17794.700) open.xlsx",
                web_url="https://x/sites/ExampleSite/Shared%20Documents/C/1/190001/DOC/md.xlsx",
            )
        ]
    )
    cand = MatterCandidate(
        name="Estate - 190001", refs=[SourceRef("clio", "C1", "X")], matter_type="estate"
    )
    d = DossierBuilder(knowledge_base=kb).build(cand, correspondence_term="190001")
    item = d.section("documents").items[0]
    assert item.record_id.startswith("sp-")
    assert " " not in item.record_id and "(" not in item.record_id  # single token
    assert "MD Mgt account (17794.700) open.xlsx" in item.summary  # filename kept in summary


# -- QuickBooks reconciliation ----------------------------------------------


class FakeQuickBooks:
    def __init__(self, *, customers=None, invoices=None, payments=None) -> None:
        self._customers = customers or []
        self._invoices = invoices or []
        self._payments = payments or []
        self.calls: list[tuple[str, Any]] = []

    def find_customers(self, name: str, *, max_results: int = 10) -> list[dict[str, Any]]:
        self.calls.append(("find", name))
        if isinstance(self._customers, Exception):
            raise self._customers
        return self._customers

    def find_customer_by_clio_id(
        self, clio_id: str, *, max_results: int = 5
    ) -> list[dict[str, Any]]:
        self.calls.append(("find_clio", clio_id))
        if isinstance(self._customers, Exception):
            raise self._customers
        return self._customers

    def list_invoices(self, *, customer_id=None, max_results: int = 100) -> list[dict[str, Any]]:
        self.calls.append(("invoices", customer_id))
        return self._invoices

    def list_payments(self, *, customer_id=None, max_results: int = 100) -> list[dict[str, Any]]:
        self.calls.append(("payments", customer_id))
        return self._payments


def test_collect_quickbooks_matches_by_clio_id() -> None:
    # _cand()'s client-contact id is "CID9"; the customer is matched by the
    # "(Clio CID9)" in its DisplayName — the client-contact id, NOT the matter id.
    qb = FakeQuickBooks(
        customers=[{"Id": "C9", "DisplayName": "Some Renamed Estate (Clio CID9)", "Balance": 100}],
        invoices=[{"Id": "I1", "DocNumber": "1001", "TotalAmt": 500, "TxnDate": "2026-01-01"}],
        payments=[{"Id": "P1", "TotalAmt": 200, "TxnDate": "2026-02-01"}],
    )
    d = DossierBuilder(quickbooks=qb).build(_cand())

    fin = [i for i in d.section("financials").items if i.system == "quickbooks"]
    ids = {i.record_id for i in fin}
    assert {"C9", "I1", "P1"} <= ids
    assert ("find_clio", "CID9") in qb.calls  # looked up by the client-contact id...
    assert ("find_clio", "123") not in qb.calls  # ...never the matter id
    assert ("invoices", "C9") in qb.calls and ("payments", "C9") in qb.calls
    assert not d.errors


def test_collect_quickbooks_ignores_wrong_clio_id_customer() -> None:
    # A customer with a different Clio id must NOT match (avoids cross-matter mixups).
    qb = FakeQuickBooks(customers=[{"Id": "CX", "DisplayName": "Smith Estate (Clio 999)"}])
    d = DossierBuilder(quickbooks=qb).build(_cand())
    assert not [i for i in d.section("financials").items if i.system == "quickbooks"]
    assert any("no customer matched" in r for r in d.sources_reviewed)


def test_collect_quickbooks_falls_back_to_name_without_clio_ref() -> None:
    cand = MatterCandidate(
        name="Smith Estate", refs=[SourceRef("estateably", "E1", "Smith")], matter_type="estate"
    )
    qb = FakeQuickBooks(customers=[{"Id": "C9", "DisplayName": "Smith Estate"}])
    d = DossierBuilder(quickbooks=qb).build(cand)
    fin = [i for i in d.section("financials").items if i.system == "quickbooks"]
    assert any(i.record_id == "C9" for i in fin)  # matched by name (no Clio ref)
    assert any(c[0] == "find" for c in qb.calls)  # used the name lookup, not find_clio


def test_reconciliation_agrees_when_matched_by_invoice_number() -> None:
    # Clio bill "555" ↔ QuickBooks invoice "clio-555"; paid = TotalAmt − Balance.
    clio = FakeClio(bills=[{"id": 1, "number": "555", "total": 500, "paid": 500, "state": "paid"}])
    qb = FakeQuickBooks(
        customers=[{"Id": "C9", "DisplayName": "Smith Estate (Clio CID9)"}],
        invoices=[{"Id": "I1", "DocNumber": "clio-555", "TotalAmt": 500, "Balance": 0}],
    )
    d = DossierBuilder(clio=clio, quickbooks=qb).build(_cand())
    rec = next(i for i in d.section("financials").items if i.system == "reconciliation")
    assert "Invoices: Clio $500.00 vs QuickBooks $500.00 — agree" in rec.summary
    assert "Payments: Clio $500.00 vs QuickBooks $500.00 — agree" in rec.summary
    assert "review" not in rec.summary  # no QuickBooks-only / unsynced exceptions


def test_reconciliation_flags_discrepancy() -> None:
    clio = FakeClio(bills=[{"id": 1, "number": "555", "total": 500, "paid": 300, "state": "paid"}])
    qb = FakeQuickBooks(
        customers=[{"Id": "C9", "DisplayName": "Smith Estate (Clio CID9)"}],
        # invoiced $50 less than Clio; Balance 150 ⇒ paid 300 (matches Clio paid).
        invoices=[{"Id": "I1", "DocNumber": "clio-555", "TotalAmt": 450, "Balance": 150}],
    )
    d = DossierBuilder(clio=clio, quickbooks=qb).build(_cand())
    rec = next(i for i in d.section("financials").items if i.system == "reconciliation")
    assert "DISCREPANCY $50.00" in rec.summary
    assert "Payments: Clio $300.00 vs QuickBooks $300.00 — agree" in rec.summary


def test_reconciliation_excludes_deleted_duplicate_bill() -> None:
    # Clio can hold a deleted duplicate of a bill number; it must NOT be summed (this
    # was the Kober ~$64,716 double-count). Only the active bill reconciles.
    clio = FakeClio(
        bills=[
            {"id": 1, "number": "10776", "total": 64716.47, "paid": 0, "state": "deleted"},
            {"id": 2, "number": "10776", "total": 61634.73, "paid": 61634.73, "state": "paid"},
        ]
    )
    qb = FakeQuickBooks(
        customers=[{"Id": "C9", "DisplayName": "Kober (Clio CID9)"}],
        invoices=[{"Id": "I1", "DocNumber": "clio-10776", "TotalAmt": 61634.73, "Balance": 0}],
    )
    d = DossierBuilder(clio=clio, quickbooks=qb).build(_cand())
    rec = next(i for i in d.section("financials").items if i.system == "reconciliation")
    assert "Invoices: Clio $61,634.73 vs QuickBooks $61,634.73 — agree" in rec.summary
    assert "64,716" not in rec.summary  # deleted duplicate excluded


def test_reconciliation_flags_quickbooks_only_invoice() -> None:
    # A "QB-<n>" invoice has no Clio bill → excluded from the matched totals and flagged.
    clio = FakeClio(bills=[{"id": 1, "number": "555", "total": 500, "paid": 500, "state": "paid"}])
    qb = FakeQuickBooks(
        customers=[{"Id": "C9", "DisplayName": "Kober (Clio CID9)"}],
        invoices=[
            {"Id": "I1", "DocNumber": "clio-555", "TotalAmt": 500, "Balance": 0},
            {"Id": "I2", "DocNumber": "QB-10890", "TotalAmt": 500, "Balance": 0},
        ],
    )
    d = DossierBuilder(clio=clio, quickbooks=qb).build(_cand())
    rec = next(i for i in d.section("financials").items if i.system == "reconciliation")
    assert "Invoices: Clio $500.00 vs QuickBooks $500.00 — agree" in rec.summary
    assert "QuickBooks-only" in rec.summary and "QB-10890" in rec.summary


def test_reconciliation_flags_clio_bill_missing_from_quickbooks() -> None:
    clio = FakeClio(
        bills=[
            {"id": 1, "number": "555", "total": 500, "paid": 500, "state": "paid"},
            {"id": 2, "number": "777", "total": 100, "paid": 0, "state": "awaiting_payment"},
        ]
    )
    qb = FakeQuickBooks(
        customers=[{"Id": "C9", "DisplayName": "Kober (Clio CID9)"}],
        invoices=[{"Id": "I1", "DocNumber": "clio-555", "TotalAmt": 500, "Balance": 0}],
    )
    d = DossierBuilder(clio=clio, quickbooks=qb).build(_cand())
    rec = next(i for i in d.section("financials").items if i.system == "reconciliation")
    assert "not found in QuickBooks" in rec.summary and "777" in rec.summary


def test_no_reconciliation_line_without_both_sides() -> None:
    # QuickBooks matched but no Clio connector → no reconciliation line (need both).
    qb = FakeQuickBooks(customers=[{"Id": "C9", "DisplayName": "Smith Estate (Clio CID9)"}])
    d = DossierBuilder(quickbooks=qb).build(_cand())
    assert not any(i.system == "reconciliation" for i in d.section("financials").items)


def test_reconciliation_is_client_level_for_multi_matter_client() -> None:
    # The client (contact CID9) has two matters; QuickBooks bills them under one
    # customer. Reconciliation matches the client's bills across matters by invoice
    # number and flags that the comparison is client-level.
    clio = FakeClio(
        client_matters=[{"id": "123"}, {"id": "456"}],
        bills_by_matter={
            "123": [{"id": 1, "number": "B1", "total": 500, "paid": 500, "state": "paid"}],
            "456": [{"id": 2, "number": "B2", "total": 200, "paid": 200, "state": "paid"}],
        },
    )
    qb = FakeQuickBooks(
        customers=[{"Id": "C9", "DisplayName": "Kober (Clio CID9)"}],
        invoices=[
            {"Id": "I1", "DocNumber": "clio-B1", "TotalAmt": 500, "Balance": 0},
            {"Id": "I2", "DocNumber": "clio-B2", "TotalAmt": 200, "Balance": 0},
        ],
    )
    d = DossierBuilder(clio=clio, quickbooks=qb).build(_cand())

    rec = next(i for i in d.section("financials").items if i.system == "reconciliation")
    assert "Invoices: Clio $700.00 vs QuickBooks $700.00 — agree" in rec.summary
    assert "client-level" in rec.summary and "2 matters" in rec.summary

    # The displayed Clio bill items stay scoped to the searched matter (123), not 456.
    clio_items = " ".join(i.summary for i in d.section("financials").items if i.system == "clio")
    assert "B1" in clio_items and "B2" not in clio_items


def test_collect_quickbooks_token_failure_is_flagged_as_error() -> None:
    # A dead QuickBooks token must NOT degrade silently (client request): it's a
    # dossier error so the UI/audit show reconciliation data was unavailable, and
    # the CONNECTOR_TOKEN_FAILURE marker (asserted here via the message) is what the
    # integration health probe watches.
    qb = FakeQuickBooks(customers=RuntimeError("QuickBooks token refresh failed: 400"))
    d = DossierBuilder(quickbooks=qb).build(_cand())

    assert any("QuickBooks unavailable" in e for e in d.errors)


def test_quickbooks_absent_leaves_no_qb_items() -> None:
    d = DossierBuilder(estateably=FakeEstateably(), clio=FakeClio()).build(_cand())
    assert not any(i.system == "quickbooks" for sec in d.sections.values() for i in sec.items)
