"""Unit tests for the AML export data-quality layer."""

from __future__ import annotations

import re

import pytest

from backend.lambdas.aml_export import quality
from backend.lambdas.aml_export.quality import OrgConfig


@pytest.fixture(autouse=True)
def _reset_quality_config():
    yield
    quality.reset_config()


def test_exclusion_reason():
    assert (
        quality.exclusion_reason(
            matter_number="190012", description="", title="DO NOT USE - Soi Cowboy", status="open"
        )
        == "Marked DO NOT USE"
    )
    assert (
        quality.exclusion_reason(
            matter_number="2100-11", description="", title="Finance & Compliance", status="open"
        )
        == "Internal / administrative matter"
    )
    assert (
        quality.exclusion_reason(
            matter_number="240099",
            description="Estate of Real",
            title="Estate of Real",
            status="open",
        )
        is None
    )
    assert quality.exclusion_reason(
        matter_number="1", description="", title="x", status="Closed"
    ).startswith("Inactive")


def test_normalize_text_collapses_whitespace_and_nbsp():
    assert quality.normalize_text("  John  Smith  ") == "John Smith"
    assert quality.normalize_text("O’Brien") == "O'Brien"
    assert quality.normalize_text(None) == ""


def test_normalize_dob_to_iso():
    assert quality.normalize_dob("1980-07-26") == "1980-07-26"
    assert quality.normalize_dob("?/?/1952") == "?/?/1952"  # unparseable passes through
    assert quality.normalize_dob(None) is None


def test_looks_like_object_id():
    assert quality.looks_like_object_id("68e7c09e0d6dab9bdce5e638") is True
    assert quality.looks_like_object_id("230009") is False


def test_finalize_flags_missing_dob_and_object_id_and_conflicts():
    parties = [
        {
            "matter_number": "230010",
            "source_record_id": "A",
            "matter_type": "Trust",
            "party_type": "Individual",
            "party_role": "Beneficiary",
            "full_legal_name": "Jane Roe",
            "date_of_birth": None,
            "description": "Jane Roe - 230010",
        },
        {
            "matter_number": "230010",
            "source_record_id": "B",
            "matter_type": "Trust",
            "party_type": "Individual",
            "party_role": "Beneficiary",
            "full_legal_name": "John Doe",
            "date_of_birth": "1980-01-01",
            "description": "John Doe - 230010",
        },
    ]
    exceptions, summary = quality.finalize(parties)
    issues = {e["issue"] for e in exceptions}
    assert any("date of birth" in i for i in issues)
    assert any("Conflicting matter number" in i for i in issues)  # same number, two records
    assert summary["conflicting_matter_numbers"] == 1
    assert summary["missing_dob"] == 1
    # exception_status is written back onto the party rows
    assert parties[0]["exception_status"]


def test_finalize_flags_near_duplicate_names():
    parties = [
        {
            "matter_number": "1",
            "source_record_id": "A",
            "party_type": "Individual",
            "party_role": "Beneficiary",
            "full_legal_name": "Carroll Lynn Wright",
            "date_of_birth": "1950-01-01",
            "description": "x",
        },
        {
            "matter_number": "2",
            "source_record_id": "B",
            "party_type": "Individual",
            "party_role": "Beneficiary",
            "full_legal_name": "Carrol Lynn Wright",
            "date_of_birth": "1950-01-01",
            "description": "x",
        },
    ]
    exceptions, _ = quality.finalize(parties)
    assert any("duplicate name" in e["issue"].lower() for e in exceptions)


# -- role taxonomy / classification (rev-2) ----------------------------------


def test_map_role_canonicalizes_and_rejects_non_principals():
    assert quality.map_role("Beneficiary") == "Beneficiary"
    assert quality.map_role("Executor Named In Will") == "Executor"
    assert quality.map_role("POA for Barry Haer Jan26 2023") == "Attorney"
    assert quality.map_role("Committee of Person") == "Committee"
    # camelCase source labels tokenize correctly and still map to canonical roles.
    assert quality.map_role("coTrustee") == "Trustee"
    assert quality.map_role("powerOfAttorney") == "Attorney"
    # Non-principals / generic roles are not screenable → None (Excluded Parties).
    assert quality.map_role("Client") is None
    assert quality.map_role("Principal") is None
    assert quality.map_role("Wealth Advisor") is None
    assert quality.map_role("accountantCpa") is None
    assert quality.map_role("Friend") is None
    assert quality.map_role("") is None


def test_is_non_principal_role_covers_camelcase_and_service_roles():
    # camelCase source labels (Estateably) must be caught regardless of party type —
    # the accountantCpa leakage in Nicole's 2026-07-27 review (item 1).
    for role in (
        "accountantCpa",
        "realEstateAgent",
        "bankRepresentative",
        "wealthAdvisor",
        "Social Worker",
        "Case Worker",
        "Case Manager",
        "Occupational Therapist",
        "Support Worker",
        "Tenant",
        "Group Home Manager",
        "Wealth Management",
        "counsel",
        "co-counsel",
        # Wealth-planning advisor titles (Nicole 2026-08-06) — a firm-plus-title label.
        "Nicola Wealth-Senior Wealth Planning Associate",
        "Wealth Planning Associate",
        "Financial Planning",
    ):
        assert quality.is_non_principal_role(role) is True, role
    # Roles already excluded before the fix stay excluded.
    for role in ("Accountant", "Portfolio Manager", "paralegal", "Investment advisor"):
        assert quality.is_non_principal_role(role) is True, role
    # Genuine principals / ambiguous roles are NOT swept in (kept for screening).
    for role in ("Beneficiary", "Client", "Principal", "brother", "", None):
        assert quality.is_non_principal_role(role) is False, role


def test_is_excluded_party_name_matches_own_company_only_at_start():
    assert quality.is_excluded_party_name("Aphrodite")
    assert quality.is_excluded_party_name("Aphrodite Ltd.")
    # A client trust that merely contains the words is NOT suppressed.
    assert quality.is_excluded_party_name("Smith Family Aphrodite") is None
    assert quality.is_excluded_party_name("Jane Doe") is None


def test_entity_detection_and_type():
    assert quality.looks_like_entity("Norah Rose Alaqrawi Trust") is True
    assert quality.looks_like_entity("Otter Tail County Historical Society") is True
    assert quality.looks_like_entity("Margaret Whitfield") is False
    assert quality.entity_party_type("Norah Rose Alaqrawi Trust") == "Trust"
    assert quality.entity_party_type("Whitfield Holdings Ltd.") == "Corporation"


def test_spelling_flag_catches_country_vs_county():
    _, issues = quality._clean_name("OTTER TAIL COUNTRY HISTORICAL SOCIETY")
    assert any("spelling error" in i for i in issues)
    assert any("Country" in i or "country" in i for i in issues)
    # No false positive on a normal county-free name.
    _, clean_issues = quality._clean_name("Otter Tail Society")
    assert not any("spelling error" in i for i in clean_issues)


def test_trust_roster_exceptions_one_per_trust_matter():
    parties = [
        {"matter_type": "Trust", "matter_number": "230010", "source_record_id": "A"},
        {"matter_type": "Trust", "matter_number": "230010", "source_record_id": "A"},
        {"matter_type": "Estate", "matter_number": "240001", "source_record_id": "B"},
    ]
    rows = quality.trust_roster_exceptions(parties)
    assert len(rows) == 1
    assert rows[0]["issue"].startswith("Party roster incomplete")


def test_missing_matter_number_wording():
    party = {
        "matter_number": None,
        "party_type": "Individual",
        "full_legal_name": "Jane Roe",
        "date_of_birth": "1980-01-01",
        "description": "",
    }
    issues = quality._party_issues(party)
    assert any("Matter number missing" in i for i in issues)


def test_configure_changes_own_company_match():
    quality.configure(OrgConfig(own_company_names=("Acme Trust",)))
    # Different casing than configured, to pin the existing case-insensitive match.
    assert quality.is_excluded_party_name("ACME TRUST LTD.") is not None
    assert quality.is_excluded_party_name("Aphrodite") is None


def test_configure_invalid_regex_raises_at_call_time():
    quality.configure(OrgConfig(internal_number_pattern="("))  # unbalanced group
    with pytest.raises(re.error):
        quality.exclusion_reason(matter_number="1", description="", title="", status="open")


def test_configure_empty_own_company_excludes_nothing():
    quality.configure(OrgConfig(own_company_names=()))
    assert quality.is_excluded_party_name("Aphrodite") is None
    assert quality.is_excluded_party_name("Anything") is None


def test_configure_changes_internal_title_keywords():
    quality.configure(OrgConfig(internal_title_keywords=("custom-internal-marker",)))
    assert quality.exclusion_reason(
        matter_number="1", description="", title="custom-internal-marker", status="open"
    ) == "Internal / administrative matter"
    assert quality.exclusion_reason(
        matter_number="190012", description="", title="Finance & Compliance", status="open"
    ) is None  # old default keyword no longer matches once overridden
    # (matter_number is a normal, non-internal number here, to isolate the
    # title-keyword override from the still-default internal number pattern.)


def test_configure_changes_internal_number_pattern():
    quality.configure(OrgConfig(internal_number_pattern=r"^\s*9999-\d+"))
    assert quality.exclusion_reason(
        matter_number="9999-1", description="", title="", status="open"
    ) == "Internal / administrative matter"


def test_reset_config_restores_defaults():
    quality.configure(OrgConfig(own_company_names=("Acme Trust",)))
    quality.reset_config()
    assert quality.is_excluded_party_name("Aphrodite") is not None
