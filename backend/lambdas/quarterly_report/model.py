"""In-memory data model for a quarterly trust statement.

The generator (:mod:`generator`) produces a :class:`ReportModel` from the Clio
connector; the renderers (:mod:`render_docx`, :mod:`render_pdf`) consume it. Money
is carried as :class:`~decimal.Decimal` so rounding stays exact; formatting to
"$8,450.25" happens only at render time.

The model deliberately separates the two artifacts the client's sample defines —
the **cover letter** (header + recipients + financial-position summary) and the
**trust ledger** (the transaction table) — while sharing the computed balances.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .period import ReportingPeriod


@dataclass
class AttorneyContact:
    """The "Reply to" trust officer / responsible attorney on the letter."""

    name: str
    email: str | None = None
    phone: str | None = None


@dataclass
class Recipient:
    """A party the statement is addressed to.

    Usually a beneficiary; for a minor or mentally-incapable beneficiary it is the
    guardian/legal representative acting **on behalf of** them (``on_behalf_of``
    names that beneficiary). ``email`` is ``None`` when none is on file — the
    renderer then shows an "email not on file" placeholder rather than inventing
    one. ``hint`` carries an optional reviewer flag (e.g. a possibly-contingent
    beneficiary, or a missing guardian) shown beside the addressee.
    """

    name: str
    email: str | None = None
    address: str | None = None
    on_behalf_of: str | None = None
    hint: str | None = None


@dataclass
class LedgerEntry:
    """One row of the trust ledger enclosure (matches the sample's columns)."""

    date: str  # ISO YYYY-MM-DD
    source_dest: str  # "Source/Destination" (counterparty or description)
    reference: str  # "Reference" (e.g. invoice number)
    payer_payee: str  # "Payer/Payee"
    funds_out: Decimal | None  # debit, or None if this row is a credit
    funds_in: Decimal | None  # credit, or None if this row is a debit
    balance: Decimal | None  # running balance after the transaction


@dataclass
class SummaryRow:
    """One row of the cover letter's itemized "Assets Held" table.

    ``amount`` is ``None`` when the figure is pending (the outside-counsel
    investment portfolio is a staff fill-in — see ``ReportModel.investment_pending``).
    ``detail`` is an optional sub-line under the description (the institution/counsel
    name(s), or the "as at …" valuation label for real property). ``fill_in`` marks a
    row the reviewer must complete, so the renderer highlights it.
    """

    description: str
    amount: Decimal | None
    provisional: bool = False
    detail: str | None = None
    fill_in: bool = False


@dataclass
class ReportModel:
    """Everything needed to render one matter's quarterly statement draft."""

    # --- header / addressing ---
    matter_number: str
    trust_name: str
    attorney: AttorneyContact
    recipients: list[Recipient]
    period: ReportingPeriod
    letter_date: date

    # --- trust ledger (from Clio) ---
    account_name: str
    account_type: str
    ledger: list[LedgerEntry]
    total_funds_out: Decimal
    total_funds_in: Decimal
    cash_balance: Decimal  # closing balance of the *cash-classified* trust accounts

    # --- cover-letter "Assets Held" summary ---
    summary_rows: list[SummaryRow]
    total_assets: Decimal | None  # None while the outside-counsel portfolio is pending
    investment_pending: bool = True

    # Per-category subtotals (Cash / Real Property / Investments / GICs) for the
    # manifest; the itemized rows are in ``summary_rows``.
    asset_breakdown: dict[str, Decimal] = field(default_factory=dict)
    # Trust accounts the classifier could not confidently place (shown as Cash but
    # flagged for staff — see :mod:`classification`).
    unclassified_accounts: list[str] = field(default_factory=list)
    # Real-property accounts whose recorded value is >15 months old: (name, as-at ISO).
    stale_valuations: list[tuple[str, str]] = field(default_factory=list)
    # The representative real-property valuation date (ISO), or None if no real property.
    real_property_as_at: str | None = None

    # Reviewer note when the matter spans multiple trust accounts (the ledger
    # enclosure covers the primary account; the Assets Held summary classifies each).
    other_account_note: str | None = None

    # Reviewer note about the recipient list — set when recipients come from
    # Estateably beneficiaries (confirm current-income-only) or when they could not
    # be resolved and the letter fell back to the Clio client contact.
    recipient_note: str | None = None

    # --- provenance for the manifest / audit trail ---
    source_refs: dict[str, object] = field(default_factory=dict)
