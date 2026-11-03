"""Render the quarterly cover letter (.docx) from a :class:`ReportModel`.

The layout mirrors Aphrodite's approved sample: letterhead, the file/matter
+ reply-to block, addressee, the "Re:" line, a short body, the financial-position
summary table, and the signature block. The output is an **editable** Word
document — a *draft* — because a human reviews, corrects (notably filling in the
investment total), and only then sends it. Nothing here is final or auto-sent.

Uses ``python-docx``. Kept free of I/O beyond producing bytes so it is unit-
testable without AWS.
"""

from __future__ import annotations

import io

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_COLOR_INDEX
from docx.shared import Pt

from ._fmt import PENDING, long_date, money
from .model import ReportModel

# Aphrodite letterhead (matches the sample). The reviewer's template can
# later swap this text header for a logo image — see the README open item.
_LETTERHEAD_NAME = "Aphrodite"
_LETTERHEAD_ADDRESS = (
    "220-545 Clyde Avenue, West Vancouver, BC V7T 1C5 | 778.742.5005 | aphrodite.example"
)
_SIGNOFF_COMPANY = "APHRODITE"
# Standing privacy disclosure at the foot of the letter (Nicole, 2026-08-11).
_PRIVACY_NOTICE = "Our privacy practices are described at www.aphrodite.example/privacy."


def render_cover_letter(model: ReportModel) -> bytes:
    """Build the cover-letter .docx and return it as bytes."""
    doc = Document()
    _letterhead(doc)
    _reply_block(doc, model)
    doc.add_paragraph(long_date(model.letter_date))

    emails = ", ".join(r.email for r in model.recipients if r.email)
    if emails:
        doc.add_paragraph(f"BY EMAIL: {emails}")

    for recipient in model.recipients:
        line = recipient.name
        if recipient.on_behalf_of:
            line = f"{recipient.name} — as representative for {recipient.on_behalf_of}"
        doc.add_paragraph(line)
        if recipient.address:
            doc.add_paragraph(recipient.address)
        # Never invent an email: show a highlighted placeholder for staff to fill in.
        if not recipient.email:
            placeholder = doc.add_paragraph().add_run("[email not on file — please add]")
            placeholder.italic = True
            _highlight(placeholder)
        if recipient.hint:
            doc.add_paragraph().add_run(f"[{recipient.hint}]").italic = True

    salutation = _salutation(model)
    doc.add_paragraph(f"Dear {salutation},")

    subject = doc.add_paragraph()
    subject.add_run(f"Re: {model.period.label} Trust Statement - {model.trust_name}").bold = True

    doc.add_paragraph(
        "We are pleased to provide you with the Trust's financial statement for the "
        f"{model.period.label.lower()} of {model.period.year}. The enclosed report "
        f"outlines the activity in the Trust account from {long_date(model.period.start)} "
        f"through {long_date(model.period.end)}, including income received, expenses "
        "paid, and the current market value of the Trust's Assets."
    )
    doc.add_paragraph(
        f"Below is a summary of the assets held by the Trust as of "
        f"{long_date(model.period.end)}."
    )

    _summary_table(doc, model)

    doc.add_paragraph(
        "Please take a moment to review the attached ledgers and statements. If you "
        "have any questions about the activity or balances shown, or if you would like "
        "additional details regarding specific transactions, feel free to contact our "
        "office."
    )
    doc.add_paragraph(
        "Thank you for your continued cooperation. We look forward to keeping you "
        "updated on the Trust's administration throughout the year."
    )

    _signoff(doc, model)
    return _to_bytes(doc)


def _letterhead(doc: Document) -> None:
    name = doc.add_paragraph()
    run = name.add_run(_LETTERHEAD_NAME)
    run.bold = True
    run.font.size = Pt(18)
    addr = doc.add_paragraph(_LETTERHEAD_ADDRESS)
    addr.runs[0].font.size = Pt(8)


def _reply_block(doc: Document, model: ReportModel) -> None:
    table = doc.add_table(rows=0, cols=2)
    _add_kv(table, f"Our File: Matter {model.matter_number}", f"Reply to: {model.attorney.name}")
    if model.attorney.email:
        _add_kv(table, model.trust_name, f"Email: {model.attorney.email}")
    else:
        _add_kv(table, model.trust_name, "")
    if model.attorney.phone:
        _add_kv(table, "", f"Phone: {model.attorney.phone}")


def _summary_table(doc: Document, model: ReportModel) -> None:
    table = doc.add_table(rows=1, cols=2)
    table.style = "Table Grid"
    header = table.rows[0].cells
    header[0].paragraphs[0].add_run("Assets Held").bold = True
    header[1].paragraphs[0].add_run(f"Balance as of {long_date(model.period.end)}").bold = True
    for row in model.summary_rows:
        cells = table.add_row().cells
        label = cells[0].paragraphs[0].add_run(row.description)
        # The institution/counsel name(s) or the "as at …" valuation label (Decision E/D).
        if row.detail:
            detail = cells[0].add_paragraph().add_run(row.detail)
            detail.italic = True
            detail.font.size = Pt(8)
        amount = cells[1].paragraphs[0].add_run(money(row.amount))
        # Emphasize the Total line.
        if row.description.lower().startswith("total"):
            label.bold = True
            amount.bold = True
        # Highlight a required staff fill-in so it can't be missed (Decision B).
        if row.fill_in:
            _highlight(amount)


def _highlight(run) -> None:
    """Yellow-highlight a run — used for the outside-counsel portfolio fill-in."""
    run.font.highlight_color = WD_COLOR_INDEX.YELLOW


def _signoff(doc: Document, model: ReportModel) -> None:
    doc.add_paragraph("Yours truly,")
    company = doc.add_paragraph()
    company.add_run(_SIGNOFF_COMPANY).bold = True
    doc.add_paragraph("Per:")
    doc.add_paragraph(model.attorney.name)
    doc.add_paragraph(_SIGNOFF_COMPANY)
    doc.add_paragraph("Encl.")
    doc.add_paragraph("cc: Client")
    tag = doc.add_paragraph(f"{{{model.matter_number}}}")
    tag.alignment = WD_ALIGN_PARAGRAPH.LEFT
    # Privacy disclosure at the foot of the letter. Added before the reviewer
    # checklist so it stays in the letter once that DRAFT block is removed.
    privacy = doc.add_paragraph(_PRIVACY_NOTICE)
    privacy.runs[0].font.size = Pt(8)
    _draft_checklist(doc, model)


def _draft_checklist(doc: Document, model: ReportModel) -> None:
    """Reviewer checklist + attention flags (Decisions B/C/D). Removed before sending."""
    heading = doc.add_paragraph()
    heading.add_run("DRAFT — REVIEWER CHECKLIST (remove before sending)").bold = True

    checklist = [
        "Custodian statement attached: Y / N",  # Decision C
        f"Outside-counsel investment portfolio total entered (currently {PENDING})",  # Decision B
        "Total Assets computed after entering the portfolio total",
    ]
    for item in checklist:
        doc.add_paragraph(f"[ ] {item}").runs[0].italic = True

    # Recipient list — confirm current-income beneficiaries only, or note the fallback.
    if model.recipient_note:
        doc.add_paragraph().add_run(f"! {model.recipient_note}").italic = True

    # Decision D — real property recorded value flagged as over 15 months old.
    for name, as_at in model.stale_valuations:
        para = doc.add_paragraph()
        para.add_run(
            f"! Real property \"{name}\" value is as at {as_at} (over 15 months old) — "
            "verify the current value before sending."
        ).italic = True

    # Decision A — accounts the classifier could not confidently place.
    for name in model.unclassified_accounts:
        para = doc.add_paragraph()
        para.add_run(
            f"! Account \"{name}\" could not be confidently classified — shown under Cash "
            "Accounts; verify its category."
        ).italic = True

    if model.other_account_note:
        para = doc.add_paragraph()
        para.add_run(f"Note: {model.other_account_note}").italic = True


def _salutation(model: ReportModel) -> str:
    names = [r.name for r in model.recipients if r.name]
    if not names:
        return "Sir or Madam"
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return " and ".join(names)
    # A beneficiary statement can list many recipients; naming them all makes an
    # unwieldy salutation, so use a generic one the reviewer refines after pruning.
    return "Beneficiaries"


def _add_kv(table, left: str, right: str) -> None:
    cells = table.add_row().cells
    cells[0].text = left
    cells[1].text = right


def _to_bytes(doc: Document) -> bytes:
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()
