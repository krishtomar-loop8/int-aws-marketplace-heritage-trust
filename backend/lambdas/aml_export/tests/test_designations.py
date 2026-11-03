"""Tests for the Aphrodite designation-list parser + lookups."""

from __future__ import annotations

from openpyxl import Workbook

from backend.lambdas.aml_export.designations import Designations, parse_designations


def _wb(sheets: dict[str, list[list]]) -> Workbook:
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    return wb


def test_parse_two_section_workbook():
    wb = _wb(
        {
            "Matters": [
                ["Matter Number", "Designation", "Reason"],
                ["240099", "include", "confirmed active"],
                ["2100-50", "exclude", "internal ops"],
            ],
            "Parties": [
                ["Party Name", "Reason"],
                ["ABC Advisory Group", "service provider"],
            ],
        }
    )
    d = parse_designations(wb)
    assert d.matter_decision("240099") == ("include", "confirmed active")
    assert d.matter_decision("2100-50")[0] == "exclude"
    assert d.has_matters
    assert d.covers_matter("240099") and not d.covers_matter("999999")
    assert d.party_excluded("ABC Advisory Group") == "service provider"
    assert d.party_excluded("Someone Else") is None


def test_empty_designations_is_backstop_only():
    d = Designations()
    assert not d.has_matters
    assert d.matter_decision("240099") == (None, None)
    assert d.party_excluded("Anyone") is None


def test_party_exclusion_defaults_reason_when_blank():
    wb = _wb({"Parties": [["Party Name"], ["No Reason Co"]]})
    d = parse_designations(wb)
    assert d.party_excluded("No Reason Co")  # non-empty default reason
