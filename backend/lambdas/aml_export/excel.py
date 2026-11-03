"""openpyxl writer for the AML export workbook.

One-row-per-party layout: a ``Parties`` sheet (the screening set), plus a
``Summary`` stat sheet, an ``Excluded Matters`` sheet (with reasons), an
``Exceptions`` sheet (the validation findings), and an ``Instructions`` sheet.
No SIN is written anywhere. The ``Parties`` sheet name is deliberately outside
the screener's reserved {Estates,Trusts,POA} set so it routes to the screener's
flexible one-row-per-party parser.
"""

from __future__ import annotations

import io
from datetime import date, datetime
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

HEADER_FONT = Font(bold=True)
TITLE_FONT = Font(bold=True, size=12)
DATE_FORMAT = "yyyy-mm-dd"

PARTY_COLUMNS = [
    ("matter_number", "Matter Number"),
    ("source_record_id", "Source Record ID"),
    ("matter_type", "Matter Type"),
    ("active", "Active/Inactive"),
    ("screening_required", "Screening Required"),
    ("party_type", "Party Type"),
    ("party_role", "Party Role"),
    ("source_role", "Source Role"),
    ("full_legal_name", "Full Legal Name"),
    ("alias", "Alias / Alternate Name"),
    ("date_of_birth", "Date of Birth"),
    ("jurisdiction", "Jurisdiction / Registration"),
    ("source", "Source"),
    ("exception_status", "Exception / Verification Status"),
]

EXCLUDED_COLUMNS = [
    ("matter_number", "Matter Number"),
    ("source_record_id", "Source Record ID"),
    ("matter_type", "Matter Type"),
    ("description", "Description"),
    ("reason", "Reason Excluded"),
]

# Parties removed from the screening set (non-taxonomy roles + Aphrodite's own
# company), kept for audit per Aphrodite decision D / item 7.
EXCLUDED_PARTY_COLUMNS = [
    ("matter_number", "Matter Number"),
    ("source_record_id", "Source Record ID"),
    ("matter_type", "Matter Type"),
    ("party_type", "Party Type"),
    ("full_legal_name", "Full Legal Name"),
    ("source_role", "Source Role"),
    ("reason", "Reason Excluded"),
]

EXCEPTION_COLUMNS = [
    ("matter_number", "Matter Number"),
    ("source_record_id", "Source Record ID"),
    ("party", "Party"),
    ("role", "Role"),
    ("issue", "Issue"),
]

# Missing-DOB worklist for source remediation (decision F).
MISSING_DOB_COLUMNS = [
    ("matter_number", "Matter Number"),
    ("matter_type", "Matter Type"),
    ("party_role", "Party Role"),
    ("full_legal_name", "Full Legal Name"),
    ("source", "Source"),
]

_WIDE_KEYS = {
    "description",
    "full_legal_name",
    "exception_status",
    "issue",
    "source",
    "reason",
    "source_role",
}


def _write_sheet(ws: Worksheet, columns: list[tuple[str, str]], rows: list[dict[str, Any]]) -> None:
    for col_idx, (_, header) in enumerate(columns, start=1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = HEADER_FONT
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    ws.freeze_panes = "A2"

    for row_idx, row in enumerate(rows, start=2):
        for col_idx, (key, _) in enumerate(columns, start=1):
            value = row.get(key)
            # Dates of birth are written as YYYY-MM-DD *text* (Aphrodite item 8b):
            # normalize_dob already produced the string, so no date coercion here.
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            if isinstance(value, (date, datetime)):
                cell.number_format = DATE_FORMAT

    for col_idx, (key, header) in enumerate(columns, start=1):
        width = max(len(header), 12)
        if key in _WIDE_KEYS:
            width = max(width, 32)
        ws.column_dimensions[get_column_letter(col_idx)].width = min(width, 45)


def _write_summary_sheet(ws: Worksheet, summary: dict[str, Any]) -> None:
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 16
    r = 1

    def line(label: str, value: Any) -> None:
        nonlocal r
        ws.cell(row=r, column=1, value=label)
        ws.cell(row=r, column=2, value=value)
        r += 1

    def section(title: str) -> None:
        nonlocal r
        cell = ws.cell(row=r, column=1, value=title)
        cell.font = HEADER_FONT
        r += 1

    section("AML Export — Summary")
    line("Total parties (screened)", summary.get("total_parties", 0))
    line("  of which 'Role to be Confirmed'", summary.get("role_to_be_confirmed", 0))
    line("Excluded parties (not screened)", summary.get("excluded_parties_count", 0))
    line("Excluded matters", summary.get("excluded_count", 0))
    line("Trust matters — roster incomplete", summary.get("roster_incomplete_count", 0))
    line("Exceptions raised", summary.get("exceptions_count", 0))
    line("Missing dates of birth", summary.get("missing_dob", 0))
    line("Incomplete legal names", summary.get("incomplete_names", 0))
    line("Conflicting matter numbers", summary.get("conflicting_matter_numbers", 0))
    line("Parties with no matter number", summary.get("unverified_matters", 0))
    line("Records needing manual review", summary.get("records_needing_review", 0))
    r += 1
    # Coverage flags — counted from the Exceptions tab, so these tie out to it exactly.
    section("Coverage flags (also on the Exceptions tab)")
    line("Present in Clio only (no Estateably file)", summary.get("clio_only_matters", 0))
    line("Present in Estateably only (no Clio matter)", summary.get("estateably_only_matters", 0))
    line("Not on Aphrodite designation list", summary.get("not_on_designation_list", 0))
    r += 1
    section("Matters by type (distinct verified matter number)")
    for k, v in sorted(summary.get("matters_by_type", {}).items()):
        line(k, v)
    r += 1
    section("Parties by role")
    for k, v in sorted(summary.get("parties_by_role", {}).items()):
        line(k, v)
    r += 1
    section("Parties by type")
    for k, v in sorted(summary.get("parties_by_party_type", {}).items()):
        line(k, v)


INSTRUCTIONS_TEXT = [
    "AML Name Export — one row per party (person or entity).",
    "",
    "This workbook lists every screening-eligible party across our active matters,",
    "one row each, for sanctions screening. It replaces the previous per-matter",
    "layout that capped beneficiaries at six columns.",
    "",
    "Sheets:",
    "  • Parties — the screening set: one row per party, with matter number, role,",
    "    party type, full legal name, and date of birth (YYYY-MM-DD text).",
    "  • Summary — counts by matter type, party role, and party type; data-quality counts.",
    "  • Excluded Matters — closed / inactive / DO NOT USE / internal-administrative",
    "    matters kept out of screening, each with the reason.",
    "  • Excluded Parties — parties removed from the screening set (non-principal roles",
    "    such as advisors/service providers/social contacts, and Aphrodite",
    "    itself), retained with a reason for audit.",
    "  • Exceptions — records needing verification (missing DOB, matter-number issues,",
    "    possible duplicates, party roster incomplete, source unavailable, etc.).",
    "  • Missing DOB — the missing-date-of-birth worklist, sorted by matter and role",
    "    (beneficiaries and principals first) for source data entry.",
    "",
    "Screening set (Party Role) is limited to Aphrodite's taxonomy: Deceased, Settlor,",
    "Trustee, Executor, Beneficiary, Protector, Attorney, Committee, UBO, Counterparty.",
    "The original source role text is preserved in the separate Source Role column; any",
    "role that does not map to the taxonomy is moved to the Excluded Parties sheet.",
    "",
    "Notes / known source-data limits (see the Exceptions sheet):",
    "  • SIN is intentionally not included in this screening export in any form.",
    "  • Matter numbers are never inferred from a case title; where neither Clio nor",
    "    Estateably holds a structured number the field is left blank and flagged.",
    "  • 'Matters by type' counts DISTINCT VERIFIED MATTER NUMBERS on the Parties sheet",
    "    (not source records); parties with no matter number are counted separately.",
    "  • Trust matters are flagged 'Party roster incomplete' until settlor/trustee/",
    "    beneficiary contacts are fully entered at source.",
    "  • Aliases, dedicated protector/UBO/counterparty rosters, and per-party",
    "    jurisdiction are not exposed by the source systems and are left blank/flagged.",
    "  • The internal Source Record ID is kept separate from the Matter Number.",
    "",
    "This export is for Aphrodite review; it is not sent to any screening provider",
    "automatically.",
]


def _write_instructions_sheet(ws: Worksheet) -> None:
    for row_idx, text in enumerate(INSTRUCTIONS_TEXT, start=1):
        ws.cell(row=row_idx, column=1, value=text or None)
    ws.column_dimensions["A"].width = 100


def build_workbook(export_result: dict[str, Any]) -> Workbook:
    """Build the AML export workbook (Parties + Summary + Excluded + Exceptions + Instructions)."""
    wb = Workbook()
    wb.remove(wb.active)

    _write_sheet(wb.create_sheet("Parties"), PARTY_COLUMNS, export_result.get("parties", []))
    _write_summary_sheet(wb.create_sheet("Summary"), export_result.get("summary", {}))
    _write_sheet(
        wb.create_sheet("Excluded Matters"), EXCLUDED_COLUMNS, export_result.get("excluded", [])
    )
    _write_sheet(
        wb.create_sheet("Excluded Parties"),
        EXCLUDED_PARTY_COLUMNS,
        export_result.get("excluded_parties", []),
    )
    _write_sheet(
        wb.create_sheet("Exceptions"), EXCEPTION_COLUMNS, export_result.get("exceptions", [])
    )
    _write_sheet(
        wb.create_sheet("Missing DOB"),
        MISSING_DOB_COLUMNS,
        export_result.get("missing_dob_list", []),
    )
    _write_instructions_sheet(wb.create_sheet("Instructions"))
    return wb


def build_workbook_bytes(export_result: dict[str, Any]) -> bytes:
    """Build the workbook and return it serialized as .xlsx bytes."""
    buffer = io.BytesIO()
    build_workbook(export_result).save(buffer)
    return buffer.getvalue()
