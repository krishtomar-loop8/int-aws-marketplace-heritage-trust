"""Unit tests for the docx/pdf renderers. No AWS/network.

The docx is parsed back with python-docx and its text asserted. The pdf is
rendered uncompressed so the drawn text is searchable in the raw bytes.
"""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

from docx import Document

from backend.lambdas.quarterly_report.model import (
    AttorneyContact,
    LedgerEntry,
    Recipient,
    ReportModel,
    SummaryRow,
)
from backend.lambdas.quarterly_report.period import ReportingPeriod
from backend.lambdas.quarterly_report.render_docx import LetterheadConfig, render_cover_letter
from backend.lambdas.quarterly_report.render_pdf import render_trust_ledger


def _model() -> ReportModel:
    return ReportModel(
        matter_number="100001",
        trust_name="Sample Family Trust",
        attorney=AttorneyContact(name="Jane Doe", email="jane@aphrodite.example"),
        recipients=[
            Recipient(
                name="Alex Sample and Jamie Sample",
                email="asample@example.com",
                address="100-123 Main Street, Vancouver, BC V6B 1A1",
            )
        ],
        period=ReportingPeriod(2026, 1),
        letter_date=date(2026, 5, 11),
        account_name="Aphrodite Main Trust Account",
        account_type="Trust",
        ledger=[
            LedgerEntry(
                "2026-02-11",
                "Sample Wealth Management Ltd",
                "",
                "Sample Wealth Management Ltd",
                None,
                Decimal("10000.00"),
                Decimal("10095.00"),
            ),
            LedgerEntry(
                "2026-02-11",
                "",
                "Invoice 26000001",
                "Law Firm A LLP",
                Decimal("1200.00"),
                None,
                Decimal("8895.00"),
            ),
            LedgerEntry(
                "2026-03-09",
                "",
                "Invoice 26000003",
                "Law Firm A LLP",
                Decimal("144.75"),
                None,
                Decimal("8450.25"),
            ),
        ],
        total_funds_out=Decimal("1644.75"),
        total_funds_in=Decimal("10000.00"),
        cash_balance=Decimal("8450.25"),
        summary_rows=[
            SummaryRow("Cash Accounts", Decimal("8450.25")),
            SummaryRow(
                "Real Property",
                Decimal("500000.00"),
                detail="as at January 10, 2024, per most recent assessment/appraisal",
            ),
            SummaryRow(
                "Investments", Decimal("250000.00"), detail="External Bank/Investment Account"
            ),
            SummaryRow(
                "Investment Portfolio - outside investment counsel "
                "(enter total from attached statement)",
                None,
                provisional=True,
                fill_in=True,
            ),
            SummaryRow("Total Assets", None, provisional=True),
        ],
        total_assets=None,
        investment_pending=True,
        asset_breakdown={
            "Cash": Decimal("8450.25"),
            "Real Property": Decimal("500000.00"),
            "Investments": Decimal("250000.00"),
            "GICs": Decimal("0"),
        },
        unclassified_accounts=["Special Holding 9001"],
        stale_valuations=[("Assets Under Administration - Real Property", "2024-01-10")],
        real_property_as_at="2024-01-10",
    )


def _docx_text(data: bytes) -> str:
    doc = Document(io.BytesIO(data))
    paras = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            paras.extend(cell.text for cell in row.cells)
    return "\n".join(paras)


def _all_runs(doc):
    """Every run in the document (body paragraphs + table cells)."""
    for para in doc.paragraphs:
        yield from para.runs
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    yield from para.runs


def test_cover_letter_is_valid_docx_with_expected_content():
    data = render_cover_letter(_model())
    # A .docx is a zip; python-docx opening it round-trips validity.
    text = _docx_text(data)
    assert "Aphrodite" in text
    assert "Matter 100001" in text
    assert "Jane Doe" in text
    assert "Re: First Quarter Trust Statement - Sample Family Trust" in text
    assert "May 11, 2026" in text
    assert "BY EMAIL: asample@example.com" in text
    assert "Alex Sample and Jamie Sample" in text
    assert "100-123 Main Street" in text
    # Itemized Assets Held table: cash + real property (with valuation label) +
    # investments, the outside-counsel fill-in, and the pending total.
    assert "Assets Held" in text
    assert "Cash Accounts" in text
    assert "$8,450.25" in text
    assert "Real Property" in text
    assert "as at January 10, 2024, per most recent assessment/appraisal" in text
    assert "Investments" in text
    assert "$500,000.00" in text
    assert "[to be confirmed]" in text
    assert "{100001}" in text


def test_cover_letter_includes_reviewer_checklist_and_custodian_line():
    text = _docx_text(render_cover_letter(_model()))
    assert "REVIEWER CHECKLIST" in text
    assert "Custodian statement attached: Y / N" in text
    assert "portfolio total entered" in text.lower()


def test_cover_letter_includes_privacy_notice():
    text = _docx_text(render_cover_letter(_model()))
    assert "www.aphrodite.example/privacy" in text


def test_render_cover_letter_custom_letterhead():
    letterhead = LetterheadConfig(
        name="Acme Trust",
        address="1 Main St",
        signoff="ACME TRUST",
        privacy_notice="See acme.example/privacy.",
    )
    text = _docx_text(render_cover_letter(_model(), letterhead))
    assert "Acme Trust" in text
    assert "1 Main St" in text
    assert "ACME TRUST" in text
    assert "acme.example/privacy" in text
    assert "Aphrodite" not in text


def test_render_cover_letter_default_letterhead_unchanged():
    text = _docx_text(render_cover_letter(_model()))
    assert "Aphrodite" in text
    assert "www.aphrodite.example/privacy" in text


def test_cover_letter_highlights_outside_counsel_fill_in():
    from docx.enum.text import WD_COLOR_INDEX

    doc = Document(io.BytesIO(render_cover_letter(_model())))
    highlighted = [
        r for r in _all_runs(doc) if r.font.highlight_color == WD_COLOR_INDEX.YELLOW
    ]
    assert highlighted, "expected a yellow-highlighted fill-in run"
    assert any("[to be confirmed]" in r.text for r in highlighted)


def test_cover_letter_flags_stale_valuation_and_unclassified_account():
    text = _docx_text(render_cover_letter(_model()))
    assert "over 15 months old" in text
    assert "2024-01-10" in text
    assert "Special Holding 9001" in text
    assert "could not be confidently classified" in text


def test_cover_letter_addresses_beneficiaries_with_guardian_and_placeholder():
    model = _model()
    model.recipients = [
        Recipient(name="Ann Adams", email="ann@example.com", address="1 King St, Victoria, BC"),
        Recipient(name="Dee Dole"),  # no email → placeholder
        Recipient(name="Pat Parent", email="pat@example.com", on_behalf_of="Kid Young"),
        Recipient(
            name="Sam Stone",
            hint="incapable beneficiary — guardian/representative not on file",
        ),
    ]
    model.recipient_note = (
        "Recipients are the beneficiaries on file in Estateably. Statements are for "
        "CURRENT INCOME beneficiaries only — confirm the list."
    )
    text = _docx_text(render_cover_letter(model))
    # BY EMAIL lists only the emails on file, in order.
    assert "BY EMAIL: ann@example.com, pat@example.com" in text
    # Guardian addressed on behalf of the minor/incapable beneficiary.
    assert "Pat Parent — as representative for Kid Young" in text
    # Missing emails show a placeholder, never an invented address.
    assert "[email not on file — please add]" in text
    assert "guardian/representative not on file" in text
    # Reviewer note lands in the draft checklist.
    assert "CURRENT INCOME beneficiaries only" in text


def test_missing_beneficiary_email_placeholder_is_highlighted():
    from docx.enum.text import WD_COLOR_INDEX

    model = _model()
    model.recipients = [Recipient(name="Dee Dole")]  # no email
    doc = Document(io.BytesIO(render_cover_letter(model)))
    highlighted = [
        r.text for r in _all_runs(doc) if r.font.highlight_color == WD_COLOR_INDEX.YELLOW
    ]
    assert any("email not on file" in t for t in highlighted)


def test_trust_ledger_is_valid_pdf():
    data = render_trust_ledger(_model())
    assert data.startswith(b"%PDF")
    assert b"%%EOF" in data
    assert len(data) > 800


def test_trust_ledger_handles_unicode_punctuation():
    # fpdf2 core fonts are latin-1; real Clio text has curly quotes/dashes/ellipsis.
    from backend.lambdas.quarterly_report.render_pdf import _safe

    assert _safe("a—b…“q”") == 'a-b..."q"'
    model = _model()
    model.account_name = "Trust – Café Société"
    model.ledger[
        0
    ].source_dest = "Prepaid Visas (6 × $500) for supplies … more text to force truncation"
    data = render_trust_ledger(model)  # must not raise
    assert data.startswith(b"%PDF")


def test_trust_ledger_contains_rows_and_totals():
    data = render_trust_ledger(_model(), compress=False)
    assert b"Aphrodite Main Trust Account" in data
    assert b"Trust" in data
    assert b"8,450.25" in data  # closing balance
    assert b"1,644.75" in data  # funds-out total
    assert b"10,000.00" in data  # funds-in total
    assert b"Invoice 26000001" in data
