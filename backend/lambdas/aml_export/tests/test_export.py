"""Unit tests for the one-row-per-party AML export orchestration.

Estateably/Clio are hand-built fakes, so these run with no network/AWS.
"""

from __future__ import annotations

from typing import Any

from backend.lambdas.aml_export.designations import Designations
from backend.lambdas.aml_export.export import (
    _format_jurisdiction,
    _matter_number_from_text,
    build_estate_parties,
    classify_case,
    resolve_matter_number,
    run_export,
)
from services.http import HttpError


def _resource(resource_id: str, attributes: dict[str, Any]) -> dict[str, Any]:
    return {"id": resource_id, "attributes": attributes}


class FakeEstateably:
    def __init__(self) -> None:
        self.estates: list[dict[str, Any]] = []
        self.intakes: dict[str, dict[str, Any]] = {}
        self.beneficiaries: dict[str, list[dict[str, Any]]] = {}
        self.contacts: dict[str, list[dict[str, Any]]] = {}
        self.executors: dict[str, list[dict[str, Any]]] = {}
        self.sdms: dict[str, list[dict[str, Any]]] = {}
        self.gate_estate_id: str | None = None  # raises 402 like a billing hold
        self.raise_for_estate_id: str | None = None  # generic failure (isolation test)

    def list_estates(self) -> list[dict[str, Any]]:
        return self.estates

    def get_client_intake(self, estate_id: str) -> dict[str, Any]:
        if estate_id == self.gate_estate_id:
            raise HttpError("Missing payment for case", status=402)
        if estate_id == self.raise_for_estate_id:
            raise RuntimeError("Estateably 500")
        return self.intakes.get(estate_id, {})

    def list_beneficiaries(self, estate_id: str) -> list[dict[str, Any]]:
        return self.beneficiaries.get(estate_id, [])

    def list_contacts(self, estate_id: str) -> list[dict[str, Any]]:
        return self.contacts.get(estate_id, [])

    def list_executors(self, estate_id: str) -> list[dict[str, Any]]:
        return self.executors.get(estate_id, [])

    def list_substitute_decision_makers(self, estate_id: str) -> list[dict[str, Any]]:
        return self.sdms.get(estate_id, [])


class FakeClio:
    def __init__(self, matters: list[dict[str, Any]] | None = None) -> None:
        self.matters = matters or []
        self.details: dict[Any, dict[str, Any]] = {}
        self.relationships: dict[Any, list[dict[str, Any]]] = {}
        self.contact_details: dict[Any, dict[str, Any]] = {}  # contact id -> attrs (address)
        self.get_contact_calls = 0

    def search_matters(self, query: str, *, fields: str | None = None) -> list[dict[str, Any]]:
        del query, fields
        return self.matters

    def get_matter(self, matter_id: Any, *, fields: str | None = None) -> dict[str, Any]:
        del fields
        return self.details.get(matter_id, {})

    def get_contact(self, contact_id: Any, *, fields: str | None = None) -> dict[str, Any]:
        del fields
        self.get_contact_calls += 1
        return self.contact_details.get(contact_id, {})

    def list_relationships(
        self, matter_id: Any = None, *, fields: str | None = None
    ) -> list[dict[str, Any]]:
        del fields
        return self.relationships.get(matter_id, [])


# -- matter number -----------------------------------------------------------


def test_matter_number_parsed_from_title_takes_trailing_token():
    assert _matter_number_from_text("Aziz-e-dean Tejpar - 230009") == "230009"
    assert _matter_number_from_text("230010-01") == "230010-01"
    assert _matter_number_from_text("2100-11 Finance & Compliance") == "2100-11"
    assert _matter_number_from_text("no number here") is None


def test_resolve_matter_number_never_returns_object_id_and_never_infers_from_title():
    # Estateably "number" is a mongo object id and the title carries a number, but
    # inference is disabled (decision B): with no structured/Clio number → blank.
    attrs = {"number": "68e7c09e0d6dab9bdce5e638", "title": "Jane Roe - 230009"}
    assert resolve_matter_number(attrs, None, attrs["title"]) == (None, "none")


def test_resolve_matter_number_ignores_free_text_in_number_field():
    # A case title landing in Estateably's number field must not become a matter
    # number ("Estate of Nicola DeBenedetto" — the failure mode Nicole flagged).
    assert resolve_matter_number({"number": "Estate of Nicola DeBenedetto"}, None) == (None, "none")


def test_resolve_matter_number_prefers_structured_then_clio():
    assert resolve_matter_number({"estateNumber": "240030"}, None) == ("240030", "structured")
    assert resolve_matter_number({}, {"display_number": "00042-Smith"}) == ("00042-Smith", "clio")


# -- per-party extraction ----------------------------------------------------


def test_estate_parties_emit_one_row_per_party_with_dob():
    es = FakeEstateably()
    estate = _resource(
        "E1",
        {
            "title": "Estate of Margaret Whitfield",
            "status": "Open",
            "estateNumber": "190101",
        },
    )
    es.intakes["E1"] = {
        "attributes": {
            "firstName": "Margaret",
            "lastName": "Whitfield",
            "dateOfBirth": "1941-03-14",
        }
    }
    es.beneficiaries["E1"] = [
        {
            "attributes": {
                "firstName": "Tom",
                "lastName": "Whitfield",
                "birth": "1970-01-02",
                "typeOfBeneficiary": "familyMember",
                "sinNumber": "111-111-111",
            }
        },
        {"attributes": {"name": "Whitfield Holdings Ltd.", "typeOfBeneficiary": "legalPerson"}},
    ]
    es.executors["E1"] = [
        {
            "attributes": {
                "firstName": "Ann",
                "lastName": "Lee",
                "birth": "1960-05-05",
                "executorType": "Individual",
            }
        }
    ]

    parties, excluded = build_estate_parties(es, estate)
    assert excluded is None
    roles = {p["party_role"] for p in parties}
    assert roles == {"Deceased", "Beneficiary", "Executor"}
    deceased = next(p for p in parties if p["party_role"] == "Deceased")
    assert deceased["full_legal_name"] == "Margaret Whitfield"
    assert deceased["date_of_birth"] == "1941-03-14"
    assert deceased["matter_number"] == "190101"
    assert deceased["source_record_id"] == "E1"
    corp = next(p for p in parties if p["full_legal_name"] == "Whitfield Holdings Ltd.")
    assert corp["party_type"] == "Corporation"


def test_beneficiaries_are_not_capped():
    es = FakeEstateably()
    es.estates = [_resource("E1", {"title": "Estate of Many - 200001", "status": "Open"})]
    es.intakes["E1"] = {"attributes": {"firstName": "Many", "lastName": "Heirs"}}
    es.beneficiaries["E1"] = [
        {
            "attributes": {
                "name": f"Beneficiary {i}",
                "birth": "1990-01-01",
                "typeOfBeneficiary": "familyMember",
            }
        }
        for i in range(9)
    ]
    result = run_export(es)
    beneficiaries = [p for p in result["parties"] if p["party_role"] == "Beneficiary"]
    assert len(beneficiaries) == 9  # no 6-cap


def test_no_sin_field_anywhere_in_output():
    es = FakeEstateably()
    es.estates = [_resource("E1", {"title": "Estate of X - 200002", "status": "Open"})]
    es.intakes["E1"] = {"attributes": {"firstName": "X", "lastName": "Y", "sinNumber": "999"}}
    es.beneficiaries["E1"] = [
        {"attributes": {"name": "B", "sinNumber": "888", "typeOfBeneficiary": "familyMember"}}
    ]
    result = run_export(es)
    for party in result["parties"]:
        assert "sin" not in party
        assert "999" not in party.values() and "888" not in party.values()


# -- exclusions --------------------------------------------------------------


def test_do_not_use_and_internal_matters_are_excluded():
    es = FakeEstateably()
    es.estates = [
        _resource("T1", {"title": "190012 - DO NOT USE - Soi Cowboy Trust", "status": "Open"}),
        _resource("T2", {"title": "2100-11 Finance & Compliance", "status": "Open"}),
        _resource(
            "E9", {"title": "Estate of Real Client", "status": "Open", "estateNumber": "240099"}
        ),
    ]
    for eid in ("T1", "T2", "E9"):
        es.intakes[eid] = {"attributes": {"firstName": "A", "lastName": "B"}}
    result = run_export(es)
    reasons = {e["reason"] for e in result["excluded"]}
    assert any("DO NOT USE" in r for r in reasons)
    assert any("Internal" in r for r in reasons)
    # Only the real client's parties survive.
    assert {p["matter_number"] for p in result["parties"]} == {"240099"}


def test_inactive_matter_excluded():
    es = FakeEstateably()
    es.estates = [_resource("E1", {"title": "Estate of Closed - 200003", "status": "Closed"})]
    es.intakes["E1"] = {"attributes": {"firstName": "C", "lastName": "D"}}
    result = run_export(es)
    assert result["parties"] == []
    assert result["excluded"][0]["reason"].startswith("Inactive")


# -- billing hold ------------------------------------------------------------


def test_billing_hold_yields_source_unavailable_not_dropped():
    es = FakeEstateably()
    es.estates = [_resource("E1", {"title": "Estate of Held - 200004", "status": "Open"})]
    es.gate_estate_id = "E1"
    result = run_export(es)
    assert len(result["parties"]) == 1
    assert result["parties"][0]["source_unavailable"] is True
    assert any("Source unavailable" in e["issue"] for e in result["exceptions"])


# -- Clio-only parties -------------------------------------------------------


def test_clio_only_matter_pulls_relationships_and_keeps_person_client_flagged():
    # A person "Client" is KEPT in the screening set flagged "Role to be Confirmed"
    # (item 3 — never drop a possible principal); trustee/beneficiary come from the
    # /relationships endpoint.
    clio = FakeClio(
        matters=[
            {
                "id": 42,
                "display_number": "00042-Roe",
                "description": "Roe Family Trust",
                "status": "open",
                "client": {"name": "Sam Roe"},
            }
        ]
    )
    clio.details[42] = {
        "id": 42,
        "display_number": "00042-Roe",
        "description": "Roe Family Trust",
        "status": "open",
        "client": {"name": "Sam Roe", "type": "Person", "date_of_birth": "1955-06-06"},
    }
    clio.relationships[42] = [
        {"description": "Trustee", "contact": {"name": "Pat Roe", "type": "Person"}},
        {"description": "Beneficiary", "contact": {"name": "Roe Co", "type": "Company"}},
    ]
    result = run_export(FakeEstateably(), clio)

    assert {p["party_role"] for p in result["parties"]} == {
        "Trustee",
        "Beneficiary",
        "Role to be Confirmed",
    }
    sam = next(p for p in result["parties"] if p["full_legal_name"] == "Sam Roe")
    assert sam["party_role"] == "Role to be Confirmed"
    assert "Role to be confirmed" in sam["exception_status"]
    corp = next(p for p in result["parties"] if p["full_legal_name"] == "Roe Co")
    assert corp["party_type"] == "Corporation"


def test_clio_matter_not_double_listed_when_already_in_estateably():
    # Same matter in both systems: matched by client name → the Clio matter is
    # skipped and the estate adopts Clio's authoritative display_number.
    es = FakeEstateably()
    es.estates = [
        _resource("E1", {"title": "Estate of Tracey Robertson - 250032", "status": "Open"})
    ]
    es.intakes["E1"] = {"attributes": {"firstName": "Tracey", "lastName": "Robertson"}}
    clio = FakeClio(
        matters=[
            {
                "id": 7,
                "display_number": "250032-Robertson",
                "description": "Estate of Tracey Robertson",
                "status": "open",
                "client": {"name": "Tracey Robertson"},
            }
        ]
    )
    result = run_export(es, clio)
    sources = {p["source"] for p in result["parties"]}
    assert not any("Clio" in s for s in sources)  # not re-added from Clio
    # Single source record (the Estateably case), not double-listed.
    assert {p["source_record_id"] for p in result["parties"]} == {"E1"}
    # It adopts Clio's authoritative display_number for the matched matter.
    assert {p["matter_number"] for p in result["parties"]} == {"250032-Robertson"}


def test_same_client_estate_and_poa_are_both_kept():
    # Louise Nagle has an Estate (Estateably) and a separate POA 260014 (Clio).
    # The distinct POA must NOT be collapsed into the Estate by client-name match.
    es = FakeEstateably()
    es.estates = [_resource("E1", {"title": "Estate of Louise Nagle", "status": "Open"})]
    es.intakes["E1"] = {"attributes": {"firstName": "Louise", "lastName": "Nagle"}}
    clio = FakeClio(
        matters=[
            {
                "id": 1,
                "display_number": "250099-Nagle",
                "description": "Estate of Louise Nagle",
                "status": "open",
                "client": {"name": "Louise Nagle"},
            },
            {
                "id": 2,
                "display_number": "260014",
                "description": "POA of Louise Nagle",
                "status": "open",
                "client": {"name": "Louise Nagle"},
            },
        ]
    )
    clio.details[1] = {**clio.matters[0], "client": {"name": "Louise Nagle", "type": "Person"}}
    clio.details[2] = {**clio.matters[1], "client": {"name": "Louise Nagle", "type": "Person"}}
    result = run_export(es, clio)
    nums = {p["matter_number"] for p in result["parties"]}
    assert "260014" in nums  # the POA survives as its own matter
    assert any(str(n).startswith("250099") for n in nums)  # the Estate is still there too


def test_classify_case_by_title_keyword():
    assert classify_case(_resource("1", {"title": "Estate of Jane Doe"})) == "estate"
    assert classify_case(_resource("2", {"title": "Jane Doe Family Trust"})) == "trust"
    assert classify_case(_resource("3", {"title": "Committeeship of John Roe"})) == "poa"


# -- role taxonomy / exclusions (Nicole rev-2) -------------------------------


def _estate(es, eid, title, number, first, last, dob=None):
    es.estates.append(_resource(eid, {"title": title, "status": "Open", "estateNumber": number}))
    attrs = {"firstName": first, "lastName": last}
    if dob:
        attrs["dateOfBirth"] = dob
    es.intakes[eid] = {"attributes": attrs}


def test_aphrodite_company_is_excluded_as_a_party():
    es = FakeEstateably()
    _estate(es, "E1", "Estate of Reginald Vance", "240001", "Reginald", "Vance", "1940-01-01")
    es.executors["E1"] = [
        {"attributes": {"name": "Aphrodite", "executorType": "Corporate"}}
    ]
    result = run_export(es)
    assert "Aphrodite" not in {p["full_legal_name"] for p in result["parties"]}
    assert any("own company" in e["reason"] for e in result["excluded_parties"])


def test_jurisdiction_normalization():
    assert _format_jurisdiction("BC", "CA") == "British Columbia, Canada"
    assert _format_jurisdiction("british columbia", "Canada") == "British Columbia, Canada"
    assert _format_jurisdiction("Nova Scotia", "Canada") == "Nova Scotia, Canada"
    assert _format_jurisdiction("Hyogo", "Japan") == "Hyogo, Japan"  # mixed-case left as-is
    assert _format_jurisdiction(None, "CA") == "Canada"
    assert _format_jurisdiction("", "") == ""


def test_jurisdiction_and_alias_from_native_estateably_fields():
    # Jurisdiction: party's own address → intake (principal) → matter location fallback,
    # all normalized. Alias: willname surfaced only when it differs from the legal name
    # (Nicole 2026-08-05, Estateably has no custom fields).
    es = FakeEstateably()
    es.estates.append(
        _resource(
            "E1",
            {
                "title": "Estate of Jane Doe - 240099",
                "status": "Open",
                "estateNumber": "240099",
                "location": {"province": "british columbia", "country": "Canada"},
            },
        )
    )
    es.intakes["E1"] = {
        "attributes": {
            "firstName": "Jane",
            "lastName": "Doe",
            "province": "ontario",
            "country": "CA",
        }
    }
    es.beneficiaries["E1"] = [
        {
            "attributes": {
                "firstName": "Bob",
                "lastName": "Doe",
                "typeOfBeneficiary": "familyMember",
                "birth": "1980-01-01",
                "willname": "Bobby Doe",
                "address": {"state": "BC", "country": "CA"},
            }
        },
        {
            "attributes": {
                "name": "Carol Doe",
                "typeOfBeneficiary": "familyMember",
                "willname": "Carol Doe",
            }
        },
    ]
    es.contacts["E1"] = [
        {
            "attributes": {
                "name": "Deb Contact",
                "category": "Beneficiary",
                "addresses": [
                    {"addressType": "Mailing", "state": "ON", "country": "CA"},
                    {
                        "addressType": "Primary (Tax Residency)",
                        "state": "Nova Scotia",
                        "country": "Canada",
                    },
                ],
            }
        }
    ]
    result = run_export(es)
    by_name = {p["full_legal_name"]: p for p in result["parties"]}

    assert by_name["Jane Doe"]["jurisdiction"] == "Ontario, Canada"  # principal from intake
    assert by_name["Bob Doe"]["jurisdiction"] == "British Columbia, Canada"  # own address
    assert by_name["Bob Doe"]["alias"] == "Bobby Doe"  # willname differs → alias
    assert by_name["Carol Doe"]["jurisdiction"] == "British Columbia, Canada"  # matter fallback
    assert by_name["Carol Doe"]["alias"] == ""  # willname == legal name → no alias
    assert by_name["Deb Contact"]["jurisdiction"] == "Nova Scotia, Canada"  # primary tax-residency


def test_wealth_planning_firm_title_is_excluded_not_role_to_be_confirmed():
    # Nicole 2026-08-06: "Nicola Wealth-Senior Wealth Planning Associate" (a firm-plus-title
    # advisor label) must land on Excluded Parties, not Role to be Confirmed.
    es = FakeEstateably()
    _estate(es, "E1", "Estate of P Q", "190015", "P", "Q")
    es.contacts["E1"] = [
        {
            "attributes": {
                "name": "Hayley Martin",
                "category": "Nicola Wealth-Senior Wealth Planning Associate",
            }
        }
    ]
    result = run_export(es)
    assert "Hayley Martin" not in {p["full_legal_name"] for p in result["parties"]}
    excl = next(e for e in result["excluded_parties"] if e["full_legal_name"] == "Hayley Martin")
    assert "Non-principal role" in excl["reason"]


def test_clio_party_jurisdiction_from_contact_address_with_cache():
    # Clio-sourced parties get jurisdiction from a per-contact get_contact (the
    # matter->client expansion can't carry an address). A repeated contact id is served
    # from the run cache — get_contact is not called twice (Nicole 2026-08-06 fast-follow).
    clio = FakeClio(
        matters=[
            {
                "id": 7,
                "display_number": "260070-Vale",
                "description": "Vale Estate",
                "status": "open",
                "client": {"name": "Val Vale"},
            }
        ]
    )
    clio.details[7] = {
        "id": 7,
        "display_number": "260070-Vale",
        "description": "Vale Estate",
        "status": "open",
        "client": {"id": 100, "name": "Val Vale", "type": "Person"},
    }
    clio.relationships[7] = [
        {
            "description": "Beneficiary",
            "contact": {"id": 101, "name": "Ben Vale", "type": "Person"},
        },
        {"description": "Trustee", "contact": {"id": 101, "name": "Ben Vale", "type": "Person"}},
    ]
    clio.contact_details[100] = {"id": 100, "primary_address": {"province": "BC", "country": "CA"}}
    clio.contact_details[101] = {
        "id": 101,
        "addresses": [{"province": "Ontario", "country": "Canada"}],
    }
    result = run_export(FakeEstateably(), clio)
    by_name = {p["full_legal_name"]: p for p in result["parties"]}
    assert by_name["Val Vale"]["jurisdiction"] == "British Columbia, Canada"  # primary_address
    assert by_name["Ben Vale"]["jurisdiction"] == "Ontario, Canada"  # addresses[] fallback
    # client (100) + related contact (101, fetched once, cached for the repeat) = 2 calls
    assert clio.get_contact_calls == 2


def test_non_principal_contacts_are_excluded_but_kept_for_audit():
    es = FakeEstateably()
    _estate(es, "E1", "Estate of Y Z", "240002", "Y", "Z")
    es.contacts["E1"] = [
        {"attributes": {"name": "Paula Legal", "category": "Paralegal"}},
        {"attributes": {"name": "Wendy Wealth", "category": "Wealth Advisor"}},
        {"attributes": {"name": "Fred Friend", "category": "Friend"}},
        {"attributes": {"name": "Bea Bene", "category": "Beneficiary", "birth": "1980-02-02"}},
    ]
    result = run_export(es)
    screened = {p["full_legal_name"] for p in result["parties"]}
    assert "Bea Bene" in screened
    assert {"Paula Legal", "Wendy Wealth", "Fred Friend"}.isdisjoint(screened)
    excluded = {e["full_legal_name"] for e in result["excluded_parties"]}
    assert {"Paula Legal", "Wendy Wealth", "Fred Friend"} <= excluded


def test_camelcase_non_principal_excluded_for_both_party_types():
    # Nicole 2026-07-27 item 1: an "accountantCpa" source label must be excluded
    # whether the party is an individual or a corporation — the fix aligns the
    # mapping across camelCase labels and party types (previously 66 individual
    # accountantCpa rows leaked into screening while the corporate ones were caught).
    es = FakeEstateably()
    _estate(es, "E1", "Estate of A B", "240010", "A", "B")
    es.contacts["E1"] = [
        {"attributes": {"name": "Ann Accountant", "category": "accountantCpa"}},
        {"attributes": {"name": "Ledger CPA Services Inc", "category": "accountantCpa"}},
    ]
    result = run_export(es)
    screened = {p["full_legal_name"] for p in result["parties"]}
    assert {"Ann Accountant", "Ledger CPA Services Inc"}.isdisjoint(screened)
    excluded = {e["full_legal_name"]: e for e in result["excluded_parties"]}
    assert "Ann Accountant" in excluded
    assert "Ledger CPA Services Inc" in excluded
    assert "Non-principal role" in excluded["Ann Accountant"]["reason"]


def test_free_text_role_is_normalized_and_source_role_preserved():
    es = FakeEstateably()
    _estate(es, "E1", "Estate of R S", "240003", "R", "S")
    es.contacts["E1"] = [
        {
            "attributes": {
                "name": "Ed Exec",
                "category": "Executor Named In Will",
                "birth": "1955-01-01",
            }
        }
    ]
    result = run_export(es)
    ed = next(p for p in result["parties"] if p["full_legal_name"] == "Ed Exec")
    assert ed["party_role"] == "Executor"
    assert ed["source_role"] == "Executor Named In Will"


def test_trust_entity_is_typed_trust_and_carries_no_dob():
    clio = FakeClio(
        matters=[
            {
                "id": 5,
                "display_number": "230010",
                "description": "Norah Rose Alaqrawi Trust",
                "status": "open",
                "client": {"name": "Norah Rose Alaqrawi Trust"},
            }
        ]
    )
    clio.details[5] = {
        "id": 5,
        "display_number": "230010",
        "description": "Norah Rose Alaqrawi Trust",
        "status": "open",
        "client": {
            "name": "Norah Rose Alaqrawi Trust",
            "type": "Person",
            "date_of_birth": "2018-05-15",
        },
    }
    result = run_export(FakeEstateably(), clio)
    entity = next(e for e in result["excluded_parties"] if e["full_legal_name"].endswith("Trust"))
    assert entity["party_type"] == "Trust"  # never "Individual"


def test_every_trust_matter_is_flagged_roster_incomplete():
    clio = FakeClio(
        matters=[
            {
                "id": 9,
                "display_number": "230099",
                "description": "Smith Family Trust",
                "status": "open",
                "client": {"name": "Smith Family Trust"},
            }
        ]
    )
    clio.details[9] = {
        "id": 9,
        "display_number": "230099",
        "description": "Smith Family Trust",
        "status": "open",
        "client": {"name": "Smith Family Trust", "type": "Company"},
    }
    result = run_export(FakeEstateably(), clio)
    assert any("Party roster incomplete" in e["issue"] for e in result["exceptions"])


def test_missing_dob_list_sorted_by_matter_then_role():
    es = FakeEstateably()
    _estate(es, "E1", "Estate of Quinn Est", "240010", "Quinn", "Est")  # principal, no DOB
    es.beneficiaries["E1"] = [
        {"attributes": {"name": "Ben Ficiary", "typeOfBeneficiary": "familyMember"}}  # no DOB
    ]
    result = run_export(es)
    rows = result["missing_dob_list"]
    assert rows
    assert rows[0]["party_role"] == "Beneficiary"  # beneficiaries prioritized


def test_counsel_for_a_principal_is_excluded_not_screened_as_that_principal():
    # A free-text role that names WHOM the lawyer serves must not be screened as that
    # principal: "Counsel for the Beneficiary" / "Counsel for X, Executrix of Y" are
    # counsel (non-principal) — the leak Nicole flagged (2026-07-28). Non-principal
    # detection takes precedence over a taxonomy term the text merely name-drops.
    es = FakeEstateably()
    _estate(es, "E1", "Estate of Marta Paszner", "240040", "Marta", "Paszner")
    es.contacts["E1"] = [
        {"attributes": {"name": "Lawrence Lawyer", "category": "Counsel for the Beneficiary"}},
        {
            "attributes": {
                "name": "Cora Counsel",
                "category": "Counsel for Beatrix, Executrix of Marta Paszner Estate",
            }
        },
        {"attributes": {"name": "Bea Bene", "category": "Beneficiary", "birth": "1980-02-02"}},
    ]
    result = run_export(es)
    screened = {p["full_legal_name"] for p in result["parties"]}
    assert "Bea Bene" in screened  # a genuine beneficiary is still screened
    assert {"Lawrence Lawyer", "Cora Counsel"}.isdisjoint(screened)
    excluded = {e["full_legal_name"]: e for e in result["excluded_parties"]}
    assert "Lawrence Lawyer" in excluded and "Cora Counsel" in excluded
    assert "Non-principal role" in excluded["Lawrence Lawyer"]["reason"]


def test_ambiguous_person_role_kept_but_advisor_excluded():
    es = FakeEstateably()
    _estate(es, "E1", "Estate of Amb Ig", "240020", "Amb", "Ig")
    es.contacts["E1"] = [
        {"attributes": {"name": "Rep Person", "category": "Alternate Representative"}},
        {"attributes": {"name": "Adele Advisor", "category": "Financial Advisor"}},
    ]
    result = run_export(es)
    rep = next((p for p in result["parties"] if p["full_legal_name"] == "Rep Person"), None)
    assert rep is not None and rep["party_role"] == "Role to be Confirmed"
    assert "Adele Advisor" not in {p["full_legal_name"] for p in result["parties"]}
    assert any(e["full_legal_name"] == "Adele Advisor" for e in result["excluded_parties"])


# -- rev-3: designation list / coverage / status drop ------------------------


def test_designation_list_precedence_and_neither_flag():
    es = FakeEstateably()
    _estate(es, "E1", "Estate of A B", "240001", "A", "B")  # not on list → neither-flag
    _estate(es, "E2", "Estate of C D", "240002", "C", "D")  # on list: include
    _estate(es, "E3", "Estate of E F", "240003", "E", "F")  # on list: exclude
    d = Designations(matters={"240002": ("include", "ok"), "240003": ("exclude", "client request")})
    result = run_export(es, designations=d)
    nums = {p["matter_number"] for p in result["parties"]}
    assert "240003" not in nums  # excluded by the list
    assert {"240001", "240002"} <= nums
    assert any("client request" in e["reason"] for e in result["excluded"])
    undesignated = [
        e for e in result["exceptions"] if "not on Aphrodite designation list" in e["issue"]
    ]
    assert {e["matter_number"] for e in undesignated} == {"240001"}  # 240002 is on the list


def test_designation_party_name_exclusion():
    es = FakeEstateably()
    _estate(es, "E1", "Estate of G H", "240004", "G", "H")
    es.beneficiaries["E1"] = [
        {
            "attributes": {
                "name": "Blocked Person",
                "typeOfBeneficiary": "familyMember",
                "birth": "1980-01-01",
            }
        }
    ]
    d = Designations(parties={"blocked person": "per compliance"})
    result = run_export(es, designations=d)
    assert "Blocked Person" not in {p["full_legal_name"] for p in result["parties"]}
    assert any(
        e["full_legal_name"] == "Blocked Person" and "per compliance" in e["reason"]
        for e in result["excluded_parties"]
    )


def test_single_system_coverage_flags():
    es = FakeEstateably()
    _estate(es, "E1", "Estate of Solo Est", "240050", "Solo", "Est")  # Estateably only
    clio = FakeClio(
        matters=[
            {
                "id": 1,
                "display_number": "250060-Only",
                "description": "Clio Only Estate",
                "status": "open",
                "client": {"name": "Clio Only"},
            }
        ]
    )
    clio.details[1] = {
        "id": 1,
        "display_number": "250060-Only",
        "description": "Clio Only Estate",
        "status": "open",
        "client": {"name": "Clio Only", "type": "Person"},
    }
    result = run_export(es, clio)
    issues = [e["issue"] for e in result["exceptions"]]
    assert any("Estateably only" in i for i in issues)
    assert any("Clio only" in i for i in issues)
    # The Clio-only matter is still SCREENED (its person client, flagged) — not dropped.
    assert "Clio Only" in {p["full_legal_name"] for p in result["parties"]}
    # Summary coverage counts tie out to the Exceptions tab exactly (memo reconciles
    # to the file — Nicole 2026-07-28). Counted from the same exception rows.
    summary = result["summary"]
    assert summary["clio_only_matters"] == sum(
        1 for i in issues if i.startswith("Present in Clio only")
    )
    assert summary["estateably_only_matters"] == sum(
        1 for i in issues if i.startswith("Present in Estateably only")
    )
    assert summary["clio_only_matters"] >= 1
    assert summary["estateably_only_matters"] >= 1
    assert summary["role_to_be_confirmed"] == sum(
        1 for p in result["parties"] if p["party_role"] == "Role to be Confirmed"
    )


def test_status_drop_exception_flags_vanished_matter():
    es = FakeEstateably()
    _estate(es, "E1", "Estate of Present", "240070", "Present", "One")
    result = run_export(es, prior_matter_numbers={"240070", "240071"})
    dropped = [e for e in result["exceptions"] if "absent now" in e["issue"]]
    assert [e["matter_number"] for e in dropped] == ["240071"]
