"""Unit tests for the quarterly-report generator. Clio is faked; no AWS/network.

The fake data mirrors the shape verified against live Clio: trust transactions
carry a ``bank_account{type}``, a ``source`` counterparty, and a **signed**
``amount`` (Clio types are asset/liability, direction is the sign). Operating-
account rows must be excluded from the trust ledger.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from backend.lambdas.quarterly_report.generator import (
    ReportDataError,
    ReportGenerator,
    _split_amount,
)
from backend.lambdas.quarterly_report.period import ReportingPeriod

Q1_2026 = ReportingPeriod(2026, 1)
LETTER_DATE = date(2026, 5, 11)

_TRUST = {"id": 55, "name": "Aphrodite Main Trust Account", "type": "Trust"}
_OPERATING = {"id": 56, "name": "Aphrodite Operating Account", "type": "Operating"}


def _txn(id_, day, amount, source, desc, account=None):
    return {
        "id": id_,
        "date": day,
        "type": "liability",
        "amount": amount,  # signed: + in, - out
        "source": source,
        "description": desc,
        "bank_account": account or _TRUST,
    }


# Sample ledger + a pre-period opening credit ($95) + an operating-account fee row
# (must be excluded) + a later out-of-period txn (must be excluded from rows).
_SAMPLE_TXNS = [
    _txn("t0", "2025-12-01", "95.00", "Opening", "Opening balance"),
    _txn("t1", "2026-02-11", "10000.00", "Sample Wealth Management Ltd", "Transfer of Funds"),
    _txn("t2", "2026-02-11", "-1200.00", "Law Firm A LLP", "Invoice 26000001"),
    _txn("t3", "2026-02-26", "-300.00", "Law Firm B LLP", "Invoice 26000002"),
    _txn("t4", "2026-03-09", "-144.75", "Law Firm A LLP", "Invoice 26000003"),
    _txn("op", "2026-02-11", "1200.00", "Fee transfer", "Invoice 26000001", _OPERATING),
    _txn("t5", "2026-04-02", "-500.00", "Law Firm A LLP", "Invoice 26000004"),
]


class FakeClio:
    def __init__(self, **overrides: Any) -> None:
        self._o = overrides

    def _v(self, key: str, default: Any) -> Any:
        val = self._o.get(key, default)
        if isinstance(val, Exception):
            raise val
        return val

    def get_matter(self, _id, *, fields=None):
        return self._v(
            "matter",
            {
                "id": 100001,
                "display_number": "100001",
                "description": "Sample Family Trust",
                "client": {
                    "id": 7,
                    "name": "Alex Sample and Jamie Sample",
                    "primary_email_address": "asample@example.com",
                },
                "responsible_attorney": {
                    "id": 2,
                    "name": "Jane Doe",
                    "email": "jane@aphrodite.example",
                },
            },
        )

    def get_contact(self, _id, *, fields=None):
        return self._v(
            "contact",
            {
                "id": 7,
                "name": "Alex Sample and Jamie Sample",
                "primary_address": {
                    "street": "100-123 Main Street",
                    "city": "Vancouver",
                    "province": "BC",
                    "postal_code": "V6B 1A1",
                },
            },
        )

    def list_bank_transactions(self, bank_account_id=None, *, fields=None, **params):
        return self._v("txns", list(_SAMPLE_TXNS))


def _build(**overrides):
    return ReportGenerator(FakeClio(**overrides)).build(100001, Q1_2026, letter_date=LETTER_DATE)


def test_header_and_recipients_from_matter_and_contact():
    model = _build()
    assert model.matter_number == "100001"
    assert model.trust_name == "Sample Family Trust"
    assert model.attorney.name == "Jane Doe"
    assert model.attorney.email == "jane@aphrodite.example"
    assert len(model.recipients) == 1
    assert model.recipients[0].name == "Alex Sample and Jamie Sample"
    assert model.recipients[0].email == "asample@example.com"
    assert "100-123 Main Street" in model.recipients[0].address
    assert "Vancouver" in model.recipients[0].address


def test_ledger_excludes_operating_rows_and_matches_sample_balances():
    model = _build()
    assert model.account_name == "Aphrodite Main Trust Account"
    assert model.account_type == "Trust"
    # Four in-period trust rows (opening, operating, and later txn excluded).
    assert len(model.ledger) == 4
    assert [row.balance for row in model.ledger] == [
        Decimal("10095.00"),
        Decimal("8895.00"),
        Decimal("8595.00"),
        Decimal("8450.25"),
    ]
    first, second = model.ledger[0], model.ledger[1]
    assert first.funds_in == Decimal("10000.00") and first.funds_out is None
    assert first.source_dest == "Sample Wealth Management Ltd"  # from `source`
    assert second.funds_out == Decimal("1200.00") and second.funds_in is None
    assert second.reference == "Invoice 26000001"  # from `description`


def test_totals_and_closing_balance():
    model = _build()
    assert model.total_funds_out == Decimal("1644.75")
    assert model.total_funds_in == Decimal("10000.00")
    assert model.cash_balance == Decimal("8450.25")


def test_summary_flags_investment_as_pending():
    model = _build()
    # Single cash account ⇒ Cash Accounts, the outside-counsel fill-in, and Total.
    cash_row, invest_row, total_row = model.summary_rows
    assert cash_row.description == "Cash Accounts"
    assert cash_row.amount == Decimal("8450.25")
    assert "outside investment counsel" in invest_row.description
    assert invest_row.amount is None and invest_row.provisional and invest_row.fill_in
    assert total_row.description == "Total Assets"
    assert total_row.amount is None and total_row.provisional
    assert model.total_assets is None
    assert model.investment_pending is True
    assert model.unclassified_accounts == []
    assert model.stale_valuations == []


def test_committeeship_accounts_are_itemized_by_category():
    # A committeeship: cash + real property + external investment + GIC accounts.
    rp = {"id": 71, "name": "Assets Under Administration - Real Property", "type": "Trust"}
    inv = {"id": 72, "name": "External Bank/Investment Account", "type": "Trust"}
    gic = {"id": 73, "name": "GICs Held in Trust at RBC", "type": "Trust"}
    txns = list(_SAMPLE_TXNS) + [
        _txn("rp1", "2026-02-20", "500000.00", "BC Assessment", "Property value", rp),
        _txn("iv1", "2026-02-21", "250000.00", "RBC DS", "Investment balance", inv),
        _txn("gc1", "2026-02-22", "100000.00", "RBC", "GIC purchase", gic),
    ]
    model = _build(txns=txns)
    labelled = {r.description: r for r in model.summary_rows}
    assert model.cash_balance == Decimal("8450.25")  # cash-classified only
    assert labelled["Real Property"].amount == Decimal("500000.00")
    assert "assessment/appraisal" in labelled["Real Property"].detail
    assert labelled["Investments"].amount == Decimal("250000.00")
    assert "External Bank/Investment Account" in labelled["Investments"].detail
    assert labelled["GICs"].amount == Decimal("100000.00")
    assert model.asset_breakdown["Real Property"] == Decimal("500000.00")
    assert model.unclassified_accounts == []


def test_unclassified_account_is_flagged_not_silently_cash():
    odd = {"id": 80, "name": "Special Holding 9001", "type": "Trust"}
    txns = list(_SAMPLE_TXNS) + [_txn("sp1", "2026-02-18", "1000.00", "x", "y", odd)]
    model = _build(txns=txns)
    # Counted as Cash (so totals reconcile) but surfaced for staff.
    assert model.cash_balance == Decimal("9450.25")
    assert "Special Holding 9001" in model.unclassified_accounts


def test_stale_real_property_valuation_is_flagged():
    rp = {"id": 71, "name": "Assets Under Administration - Real Property", "type": "Trust"}
    # Last adjustment in 2024 — well over 15 months before Q1 2026 end (Mar 31, 2026).
    txns = list(_SAMPLE_TXNS) + [
        _txn("rp_old", "2024-01-10", "500000.00", "BC Assessment", "Property value", rp)
    ]
    model = _build(txns=txns)
    assert model.real_property_as_at == "2024-01-10"
    stale_names = [name for name, _ in model.stale_valuations]
    assert "Assets Under Administration - Real Property" in stale_names


def test_source_refs_capture_provenance():
    model = _build()
    assert model.source_refs["clio_matter_number"] == "100001"
    assert model.source_refs["clio_bank_account_id"] == "55"
    assert model.source_refs["ledger_row_count"] == 4
    assert model.source_refs["other_trust_accounts"] == []
    assert "Aphrodite Main Trust Account" in model.source_refs["trust_account_balances"]
    assert model.other_account_note is None  # single account ⇒ no note


def test_multiple_trust_accounts_classify_and_note_others():
    gic = {"id": 99, "name": "GIC Sub-Account", "type": "Trust"}
    txns = list(_SAMPLE_TXNS) + [_txn("g1", "2026-02-15", "2000.00", "RBC", "GIC purchase", gic)]
    model = _build(txns=txns)
    # Main account has the most rows, so its transactions form the ledger table.
    assert model.account_name == "Aphrodite Main Trust Account"
    assert "GIC Sub-Account" in model.source_refs["other_trust_accounts"]
    # The GIC sub-account is classified as GICs, not summed into Cash.
    assert model.cash_balance == Decimal("8450.25")
    assert model.summary_rows[0].amount == Decimal("8450.25")
    labelled = {r.description: r for r in model.summary_rows}
    assert labelled["GICs"].amount == Decimal("2000.00")
    # A reviewer note surfaces the multi-account situation with per-account balances.
    assert model.other_account_note and "GIC Sub-Account" in model.other_account_note


def test_no_trust_transactions_yields_empty_ledger():
    model = _build(txns=[_txn("op", "2026-02-11", "500.00", "x", "y", _OPERATING)])
    assert model.ledger == []
    assert model.cash_balance == Decimal("0")
    assert model.summary_rows[0].amount == Decimal("0")


def test_missing_matter_raises():
    with pytest.raises(ReportDataError):
        ReportGenerator(FakeClio(matter={})).build(1, Q1_2026, letter_date=LETTER_DATE)


def test_contact_fetch_failure_does_not_break_draft():
    model = _build(contact=RuntimeError("boom"))
    assert model.recipients[0].address is None
    assert model.recipients[0].name == "Alex Sample and Jamie Sample"


def test_recipient_address_collapses_multiline_street():
    # Real Clio street fields can be multi-line (e.g. a care-facility name + street).
    contact = {
        "id": 7,
        "name": "Alex Sample and Jamie Sample",
        "primary_address": {
            "street": "Creekstone Care Centre\n1526 Oxford Street",
            "city": "North Vancouver",
            "province": "BC",
            "postal_code": "V7J 1E5",
        },
    }
    model = _build(contact=contact)
    addr = model.recipients[0].address
    assert "\n" not in addr
    assert addr == "Creekstone Care Centre 1526 Oxford Street, North Vancouver, BC, V7J 1E5"


def test_split_amount_uses_sign_for_clio_asset_liability_types():
    # Clio trust types are liability/asset; direction is the sign of the amount.
    assert _split_amount({"type": "liability", "amount": "10000"}) == (None, Decimal("10000"))
    assert _split_amount({"type": "liability", "amount": "-1200"}) == (Decimal("1200"), None)
    assert _split_amount({"type": "asset", "amount": "-50"}) == (Decimal("50"), None)
    assert _split_amount({"amount": None}) == (None, None)


# -- beneficiary recipients from Estateably (Nicole 2026-08-06) ---------------


class FakeEstateably:
    def __init__(self, *, estates=None, beneficiaries=None, fail_beneficiaries=False):
        self._estates = estates or []
        self._beneficiaries = beneficiaries or {}
        self._fail = fail_beneficiaries
        self.estate_calls = 0
        self.beneficiary_calls: list[str] = []

    def list_estates(self, *, page_size=200):
        self.estate_calls += 1
        return list(self._estates)

    def list_beneficiaries(self, estate_id):
        self.beneficiary_calls.append(estate_id)
        if self._fail:
            raise RuntimeError("estateably down")
        return list(self._beneficiaries.get(estate_id, []))


def _estate(estate_id, attrs):
    return {"id": estate_id, "attributes": attrs}


def _bene(attrs):
    return {"id": attrs.get("firstName", "x") + attrs.get("lastName", ""), "attributes": attrs}


def _build_est(estateably, **clio_overrides):
    gen = ReportGenerator(FakeClio(**clio_overrides), estateably=estateably)
    return gen.build(100001, Q1_2026, letter_date=LETTER_DATE)


# Estate whose file name embeds the Clio matter number 100001.
_MATCHING_ESTATE = _estate("e1", {"fileName": "Sample Family - 100001"})


def test_beneficiaries_replace_client_when_estate_matches():
    est = FakeEstateably(
        estates=[_MATCHING_ESTATE],
        beneficiaries={
            "e1": [
                _bene({"firstName": "Ann", "lastName": "Adams", "email": "ann@example.com"}),
                _bene({"firstName": "Bob", "lastName": "Brown", "email": "bob@example.com"}),
            ]
        },
    )
    model = _build_est(est)
    names = [r.name for r in model.recipients]
    assert names == ["Ann Adams", "Bob Brown"]
    assert "Alex Sample and Jamie Sample" not in names  # client no longer the addressee
    assert model.recipients[0].email == "ann@example.com"
    assert "CURRENT INCOME" in model.recipient_note
    assert est.beneficiary_calls == ["e1"]


def test_estate_matched_by_structured_number():
    est = FakeEstateably(
        estates=[_estate("e9", {"estateNumber": "100001"})],
        beneficiaries={
            "e9": [_bene({"firstName": "Cy", "lastName": "Clark", "email": "cy@x.com"})]
        },
    )
    model = _build_est(est)
    assert [r.name for r in model.recipients] == ["Cy Clark"]


def test_missing_email_yields_placeholder_and_note_count():
    est = FakeEstateably(
        estates=[_MATCHING_ESTATE],
        beneficiaries={
            "e1": [
                _bene({"firstName": "Ann", "lastName": "Adams", "email": "ann@example.com"}),
                _bene({"firstName": "Dee", "lastName": "Dole"}),  # no email
            ]
        },
    )
    model = _build_est(est)
    dee = next(r for r in model.recipients if r.name == "Dee Dole")
    assert dee.email is None
    assert "1 recipient(s) have no email on file" in model.recipient_note


def test_minor_beneficiary_addressed_to_guardian():
    est = FakeEstateably(
        estates=[_MATCHING_ESTATE],
        beneficiaries={
            "e1": [
                _bene(
                    {
                        "firstName": "Kid",
                        "lastName": "Young",
                        "isMinor": True,
                        "representativeFirstName": "Pat",
                        "representativeLastName": "Parent",
                        "representativeEmail": "pat@example.com",
                    }
                )
            ]
        },
    )
    model = _build_est(est)
    r = model.recipients[0]
    assert r.name == "Pat Parent"
    assert r.on_behalf_of == "Kid Young"
    assert r.email == "pat@example.com"


def test_incapable_beneficiary_without_representative_is_flagged():
    est = FakeEstateably(
        estates=[_MATCHING_ESTATE],
        beneficiaries={
            "e1": [_bene({"firstName": "Sam", "lastName": "Stone", "isMentallyIncapable": True})]
        },
    )
    model = _build_est(est)
    r = model.recipients[0]
    assert r.name == "Sam Stone"
    assert r.on_behalf_of is None
    assert "guardian/representative not on file" in r.hint


def test_org_beneficiary_without_email_gets_hint():
    est = FakeEstateably(
        estates=[_MATCHING_ESTATE],
        beneficiaries={
            "e1": [_estate("o1", {"name": "Red Cross Society", "typeOfBeneficiary": "legalPerson"})]
        },
    )
    model = _build_est(est)
    r = model.recipients[0]
    assert r.name == "Red Cross Society"
    assert r.email is None
    assert "organization" in r.hint


def test_contingent_hint_from_free_form_fields():
    est = FakeEstateably(
        estates=[_MATCHING_ESTATE],
        beneficiaries={
            "e1": [
                _bene(
                    {
                        "firstName": "Rem",
                        "lastName": "Ainder",
                        "email": "rem@x.com",
                        "fields": {"interest": "Contingent"},
                    }
                )
            ]
        },
    )
    model = _build_est(est)
    assert "possibly contingent" in model.recipients[0].hint


def test_no_estateably_addresses_client():
    model = _build()  # estateably=None
    assert [r.name for r in model.recipients] == ["Alex Sample and Jamie Sample"]
    assert model.recipient_note is None


def test_estate_not_matched_falls_back_to_client_with_note():
    est = FakeEstateably(estates=[], beneficiaries={})
    model = _build_est(est)
    assert model.recipients[0].name == "Alex Sample and Jamie Sample"
    assert "not matched" in model.recipient_note


def test_estate_with_no_beneficiaries_falls_back_with_note():
    est = FakeEstateably(estates=[_MATCHING_ESTATE], beneficiaries={"e1": []})
    model = _build_est(est)
    assert model.recipients[0].name == "Alex Sample and Jamie Sample"
    assert "No beneficiaries on file" in model.recipient_note


def test_estateably_error_falls_back_to_client_with_note():
    est = FakeEstateably(estates=[_MATCHING_ESTATE], fail_beneficiaries=True)
    model = _build_est(est)
    assert model.recipients[0].name == "Alex Sample and Jamie Sample"
    assert "could not be retrieved" in model.recipient_note


def test_estate_index_is_built_once_across_matters():
    est = FakeEstateably(
        estates=[_MATCHING_ESTATE],
        beneficiaries={
            "e1": [_bene({"firstName": "Ann", "lastName": "Adams", "email": "a@x.com"})]
        },
    )
    gen = ReportGenerator(FakeClio(), estateably=est)
    gen.build(100001, Q1_2026, letter_date=LETTER_DATE)
    gen.build(100001, Q1_2026, letter_date=LETTER_DATE)
    assert est.estate_calls == 1  # memoized per generator instance
