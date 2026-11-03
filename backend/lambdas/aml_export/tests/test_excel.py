"""Tests for the openpyxl workbook writer: sheet names, headers, cell values."""

from __future__ import annotations

import io

from openpyxl import load_workbook

from backend.lambdas.aml_export.excel import build_workbook, build_workbook_bytes

_EMPTY = {
    "parties": [],
    "excluded": [],
    "excluded_parties": [],
    "exceptions": [],
    "missing_dob_list": [],
    "summary": {},
}


def _party(**over):
    base = {
        "matter_number": "190101",
        "source_record_id": "E1",
        "matter_type": "Estate",
        "active": "Active",
        "screening_required": "Yes",
        "party_type": "Individual",
        "party_role": "Deceased",
        "source_role": "deceased",
        "full_legal_name": "Margaret Whitfield",
        "alias": "",
        "date_of_birth": "1941-03-14",
        "jurisdiction": "BC, Canada",
        "source": "Estateably: client intake",
        "exception_status": "",
    }
    base.update(over)
    return base


def test_build_workbook_has_expected_sheets_in_order():
    wb = build_workbook(_EMPTY)
    assert wb.sheetnames == [
        "Parties",
        "Summary",
        "Excluded Matters",
        "Excluded Parties",
        "Exceptions",
        "Missing DOB",
        "Instructions",
    ]


def test_parties_sheet_headers():
    wb = build_workbook(_EMPTY)
    headers = [c.value for c in next(wb["Parties"].iter_rows(min_row=1, max_row=1))]
    assert headers == [
        "Matter Number",
        "Source Record ID",
        "Matter Type",
        "Active/Inactive",
        "Screening Required",
        "Party Type",
        "Party Role",
        "Source Role",
        "Full Legal Name",
        "Alias / Alternate Name",
        "Date of Birth",
        "Jurisdiction / Registration",
        "Source",
        "Exception / Verification Status",
    ]
    # No SIN anywhere.
    assert not any("SIN" in (h or "").upper() for h in headers)


def test_parties_sheet_writes_values_and_dob_as_text():
    wb = build_workbook({**_EMPTY, "parties": [_party()]})
    ws = wb["Parties"]
    values = [c.value for c in next(ws.iter_rows(min_row=2, max_row=2))]
    assert values[0] == "190101"
    assert values[1] == "E1"
    assert values[8] == "Margaret Whitfield"
    # DOB is stored as YYYY-MM-DD *text*, not an Excel date serial (item 8b).
    dob_cell = ws.cell(row=2, column=11)
    assert dob_cell.value == "1941-03-14"
    assert isinstance(dob_cell.value, str)


def test_unparseable_dob_passes_through_as_text():
    wb = build_workbook({**_EMPTY, "parties": [_party(date_of_birth="?/?/1952")]})
    assert wb["Parties"].cell(row=2, column=11).value == "?/?/1952"


def test_excluded_parties_and_missing_dob_sheets():
    result = {
        **_EMPTY,
        "excluded_parties": [
            {
                "matter_number": "240030",
                "source_record_id": "42",
                "matter_type": "Estate",
                "party_type": "Corporation",
                "full_legal_name": "Aphrodite",
                "source_role": "Executor",
                "reason": "Aphrodite — own company, not a screened party",
            }
        ],
        "missing_dob_list": [
            {
                "matter_number": "240010",
                "matter_type": "Estate",
                "party_role": "Beneficiary",
                "full_legal_name": "Ben Ficiary",
                "source": "Estateably: beneficiaries",
            }
        ],
    }
    wb = build_workbook(result)
    assert wb["Excluded Parties"].cell(row=2, column=5).value == "Aphrodite"
    assert wb["Excluded Parties"].cell(row=2, column=7).value.startswith("Aphrodite")
    assert wb["Missing DOB"].cell(row=2, column=4).value == "Ben Ficiary"


def test_excluded_and_exceptions_sheets():
    result = {
        **_EMPTY,
        "excluded": [
            {
                "matter_number": "190012",
                "source_record_id": "E9",
                "matter_type": "Trust",
                "description": "DO NOT USE",
                "reason": "Marked DO NOT USE",
            }
        ],
        "exceptions": [
            {
                "matter_number": "230010",
                "source_record_id": "B",
                "party": "Jane Roe",
                "role": "Beneficiary",
                "issue": "Missing date of birth",
            }
        ],
    }
    wb = build_workbook(result)
    # Source Record ID column distinguishes two source records that share an internal
    # matter number (e.g. two 2100-11 admin matters) — Nicole 2026-07-27 item 2.
    excl_headers = [c.value for c in next(wb["Excluded Matters"].iter_rows(min_row=1, max_row=1))]
    assert "Source Record ID" in excl_headers
    assert wb["Excluded Matters"].cell(row=2, column=2).value == "E9"
    assert wb["Excluded Matters"].cell(row=2, column=5).value == "Marked DO NOT USE"
    assert wb["Exceptions"].cell(row=2, column=5).value == "Missing date of birth"


def test_summary_sheet_renders_counts():
    summary = {
        "total_parties": 3,
        "role_to_be_confirmed": 1,
        "excluded_count": 1,
        "clio_only_matters": 7,
        "estateably_only_matters": 2,
        "not_on_designation_list": 4,
        "matters_by_type": {"Estate": 2},
        "parties_by_role": {"Deceased": 1},
        "parties_by_party_type": {"Individual": 3},
    }
    wb = build_workbook({**_EMPTY, "summary": summary})
    rows = [
        (wb["Summary"].cell(row=r, column=1).value, wb["Summary"].cell(row=r, column=2).value)
        for r in range(1, 40)
    ]
    labels = [label for label, _ in rows]
    assert "Total parties (screened)" in labels
    assert "Coverage flags (also on the Exceptions tab)" in labels
    assert "Matters by type (distinct verified matter number)" in labels
    # Reconciliation counts render with their values.
    values = dict(rows)
    assert values["  of which 'Role to be Confirmed'"] == 1
    assert values["Present in Clio only (no Estateably file)"] == 7
    assert values["Present in Estateably only (no Clio matter)"] == 2
    assert values["Not on Aphrodite designation list"] == 4


def test_build_workbook_bytes_round_trips():
    raw = build_workbook_bytes({**_EMPTY, "parties": [_party()]})
    wb = load_workbook(io.BytesIO(raw))
    assert wb["Parties"].cell(row=2, column=1).value == "190101"
