"""Render the trust ledger enclosure (.pdf) from a :class:`ReportModel`.

Reproduces the sample's ledger table — Account name/type header, one row per
transaction (Date, Source/Destination, Reference, Payer/Payee, Funds out, Funds
in, Balance), and a totals row. Uses ``fpdf2`` (pure Python — no native
dependencies, so it packages cleanly into the Lambda zip). Produces bytes; no I/O
beyond that, so it is unit-testable without AWS.
"""

from __future__ import annotations

from fpdf import FPDF

from ._fmt import long_date, money_or_dash
from .model import ReportModel

# Column headers and their relative widths (sum is scaled to the page width).
_COLUMNS = [
    ("Date", 14),
    ("Source/Destination", 26),
    ("Reference", 20),
    ("Payer/Payee", 22),
    ("Funds out ($)", 16),
    ("Funds in ($)", 16),
    ("Balance ($)", 16),
]


def render_trust_ledger(model: ReportModel, *, compress: bool = True) -> bytes:
    """Build the trust-ledger .pdf and return it as bytes.

    ``compress=False`` emits uncompressed content streams so tests can search the
    rendered text in the raw bytes.
    """
    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_compression(compress)
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_page()

    _header(pdf, model)
    _ledger_table(pdf, model)

    # fpdf2 returns a bytearray; normalize to bytes for S3/tests.
    return bytes(pdf.output())


def _header(pdf: FPDF, model: ReportModel) -> None:
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, _safe(f"Account Name: {model.account_name}"), new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, _safe(f"Account Type: {model.account_type}"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(
        0,
        6,
        _safe(
            f"{model.period.label} {model.period.year}  -  "
            f"{long_date(model.period.start)} to {long_date(model.period.end)}"
        ),
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.ln(2)


def _scaled_widths(pdf: FPDF) -> list[float]:
    usable = pdf.w - pdf.l_margin - pdf.r_margin
    total = sum(w for _, w in _COLUMNS)
    return [usable * w / total for _, w in _COLUMNS]


def _ledger_table(pdf: FPDF, model: ReportModel) -> None:
    widths = _scaled_widths(pdf)

    pdf.set_font("Helvetica", "B", 8)
    for (title, _), width in zip(_COLUMNS, widths, strict=True):
        pdf.cell(width, 7, title, border="B", align="L")
    pdf.ln()

    pdf.set_font("Helvetica", "", 8)
    for row in model.ledger:
        cells = [
            (row.date, "L"),
            (row.source_dest, "L"),
            (row.reference, "L"),
            (row.payer_payee, "L"),
            (money_or_dash(row.funds_out), "R"),
            (money_or_dash(row.funds_in), "R"),
            (money_or_dash(row.balance), "R"),
        ]
        for (text, align), width in zip(cells, widths, strict=True):
            pdf.cell(width, 6, _truncate(text, width), align=align)
        pdf.ln()

    # Totals row (Funds out / Funds in), aligned under their columns.
    pdf.set_font("Helvetica", "B", 8)
    totals = [
        "",
        "",
        "",
        "",
        money_or_dash(model.total_funds_out),
        money_or_dash(model.total_funds_in),
        "",
    ]
    aligns = ["L", "L", "L", "L", "R", "R", "R"]
    for text, align, width in zip(totals, aligns, widths, strict=True):
        pdf.cell(width, 7, text, border="T", align=align)
    pdf.ln()


# fpdf2's built-in fonts are latin-1 only. Real Clio descriptions can carry
# typographic punctuation (curly quotes, dashes, ellipsis); map the common ones
# to ASCII and replace anything else so rendering never crashes on live data.
_PUNCT = {
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "–": "-",
    "—": "-",
    "…": "...",
    " ": " ",
}


def _safe(text: str) -> str:
    """Make text renderable by a latin-1 core font (transliterate, then replace)."""
    for uni, ascii_ in _PUNCT.items():
        text = text.replace(uni, ascii_)
    return text.encode("latin-1", "replace").decode("latin-1")


def _truncate(text: str, width_mm: float) -> str:
    """latin-1-safe cell text with a crude width guard against overflow."""
    text = _safe(text)
    max_chars = max(4, int(width_mm / 1.6))
    return text if len(text) <= max_chars else text[: max_chars - 1] + "..."
