"""Assemble a :class:`ReportModel` for one matter from Clio trust accounting.

The client's approved sample is a client-facing quarterly trust statement whose
numbers come entirely from Clio's trust ledger:

- **header** (matter number, trust name, responsible attorney) ← ``get_matter``
- **recipients** ← the matter's client contact
- **trust ledger + cash balance** ← the trust ``bank_account`` and its
  ``bank_transactions`` (filtered client-side to the reporting quarter)

The **investment / "Other Assets"** figure is *not* in any connected system (it
lives on a third-party custodian statement), so it is emitted as a flagged
placeholder and the total-assets line is marked provisional. See the module
README for the field-to-source map and open items.

Clio field names are read defensively (with fallbacks) because the exact shape
per HT's configuration is confirmed against live data during the Phase 3 probe,
not here. Everything in this module is pure given a connector, so it is unit-
tested with a fake Clio and no AWS/network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from services.logging import get_logger

from ._fmt import long_date, money
from .classification import (
    CATEGORY_CASH,
    CATEGORY_GICS,
    CATEGORY_INVESTMENTS,
    CATEGORY_REAL_PROPERTY,
    DEFAULT_CLASSIFIER,
    AccountClassifier,
)
from .model import AttorneyContact, LedgerEntry, Recipient, ReportModel, SummaryRow
from .period import ReportingPeriod

logger = get_logger(__name__)

# Nicole (2026-07-21): flag a real-property valuation older than this for staff.
_STALE_VALUATION_MONTHS = 15

# Clio bank_transaction "type" values that move money *into* the trust account.
_CREDIT_TYPES = {"credit", "deposit", "trust deposit"}
# ... and *out* of it.
_DEBIT_TYPES = {"debit", "withdrawal", "trust withdrawal", "disbursement"}

# Clio bank_account "type" that marks the trust (vs. operating) account.
_TRUST_ACCOUNT_TYPE = "trust"


class ClioLike(Protocol):
    """The subset of :class:`ClioConnector` the generator needs (fakeable)."""

    def get_matter(self, matter_id: int | str, *, fields: str | None = ...) -> dict[str, Any]: ...

    def get_contact(self, contact_id: int | str, *, fields: str | None = ...) -> dict[str, Any]: ...

    def list_bank_transactions(
        self, bank_account_id: int | str | None = ..., *, fields: str | None = ..., **params: Any
    ) -> list[dict[str, Any]]: ...


class EstateablyLike(Protocol):
    """The subset of :class:`EstateablyConnector` used to address beneficiaries."""

    def list_estates(self, *, page_size: int = ...) -> list[dict[str, Any]]: ...

    def list_beneficiaries(self, estate_id: str) -> list[dict[str, Any]]: ...


class ReportDataError(Exception):
    """Raised when a matter lacks the data needed to build a statement."""


# --- small defensive extractors --------------------------------------------


def _money(value: Any) -> Decimal | None:
    """Parse a Clio money value (string or number) into an exact Decimal."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _first_email(contact: dict[str, Any]) -> str | None:
    """Pull a contact's email from the common Clio shapes."""
    direct = contact.get("primary_email_address") or contact.get("email")
    if direct:
        return str(direct)
    for entry in contact.get("email_addresses") or []:
        addr = entry.get("address") if isinstance(entry, dict) else None
        if addr:
            return str(addr)
    return None


def _format_address(contact: dict[str, Any]) -> str | None:
    """Render a contact's primary mailing address to a single line."""
    addr = contact.get("primary_address")
    if not addr:
        addresses = contact.get("addresses") or []
        addr = addresses[0] if addresses else None
    if not isinstance(addr, dict):
        return None
    parts = [
        addr.get("street"),
        addr.get("city"),
        addr.get("province"),
        addr.get("postal_code"),
    ]
    # Collapse embedded newlines/extra whitespace (real Clio street fields can be
    # multi-line, e.g. a care-facility name + street) into a clean single line.
    line = ", ".join(" ".join(str(p).split()) for p in parts if p)
    return line or None


# --- Estateably beneficiary extractors --------------------------------------
#
# Estateably is the system of record for beneficiary contact details (Nicole
# 2026-08-06). Records are JSON:API items ({id, attributes}); the fields used here
# were confirmed against the live OpenAPI spec + real HT data.

# Aphrodite matter numbers embedded in an Estateably case title/fileName (e.g.
# "Tracey Robertson - 250032") — take the trailing token. Mirrors the AML export.
_MATTER_NUM_RE = re.compile(r"\b(\d{6}(?:-\d{2})?|2100-\d{2}|999999)\b")
# typeOfBeneficiary values that are organizations (no personal email exists).
_ORG_BENEFICIARY_TYPES = {"legalperson", "trust"}


def _attrs(resource: dict[str, Any] | None) -> dict[str, Any]:
    return (resource or {}).get("attributes") or {}


def _first(attrs: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = attrs.get(key)
        if value not in (None, ""):
            return value
    return None


def _looks_like_object_id(value: str) -> bool:
    """A 24-char hex string is an Estateably/Mongo object id, not a matter number."""
    return bool(re.fullmatch(r"[0-9a-fA-F]{24}", value or ""))


def _matter_number_from_text(*texts: str | None) -> str | None:
    for text in texts:
        matches = _MATTER_NUM_RE.findall(text or "")
        if matches:
            return matches[-1]  # the number is the trailing token
    return None


def _estate_matter_number(attrs: dict[str, Any]) -> str | None:
    """The Aphrodite matter number for an estate: a structured field, else the
    trailing number parsed from its file name/title (deterministic, by number)."""
    structured = _first(
        attrs, "estateNumber", "matterNumber", "displayNumber", "number", "fileNumber"
    )
    if structured:
        s = _text(structured)
        if s and re.search(r"\d", s) and len(s) <= 20 and not _looks_like_object_id(s):
            return s
    return _matter_number_from_text(attrs.get("fileName"), attrs.get("title"), attrs.get("name"))


def _beneficiary_name(attrs: dict[str, Any]) -> str:
    parts = (attrs.get("firstName"), attrs.get("middleName"), attrs.get("lastName"))
    joined = " ".join(_text(p) for p in parts if _text(p)).strip()
    return joined or _text(attrs.get("name")) or _text(attrs.get("willname"))


def _est_address(addr: Any) -> str | None:
    """Render an Estateably address object to a single line."""
    if not isinstance(addr, dict):
        return None
    parts = [
        addr.get("unit"),
        addr.get("street"),
        addr.get("city"),
        addr.get("state") or addr.get("province"),
        addr.get("zipCode") or addr.get("postal_code"),
    ]
    line = ", ".join(" ".join(_text(p).split()) for p in parts if _text(p))
    return line or None


def _beneficiary_address(attrs: dict[str, Any]) -> str | None:
    addr = attrs.get("address")
    if not addr:
        addresses = attrs.get("addresses") or []
        addr = addresses[0] if addresses else None
    return _est_address(addr)


def _is_org_beneficiary(attrs: dict[str, Any]) -> bool:
    return _text(attrs.get("typeOfBeneficiary")).lower() in _ORG_BENEFICIARY_TYPES


def _looks_contingent(attrs: dict[str, Any]) -> bool:
    """Best-effort hint only: the free-form ``fields`` bag may flag a contingent
    beneficiary. Never authoritative (Aphrodite doesn't use custom fields) — it just
    helps the reviewer prune to current-income beneficiaries."""
    fields = attrs.get("fields")
    if not isinstance(fields, dict):
        return False
    if fields.get("contingentBeneficiary") in (True, "true", "True"):
        return True
    return _text(fields.get("interest")).lower() == "contingent"


def _beneficiary_to_recipients(attrs: dict[str, Any]) -> list[Recipient]:
    """Turn one Estateably beneficiary into the party the letter addresses.

    Normally the beneficiary themselves. For a minor or mentally-incapable
    beneficiary the guardian/legal representative is addressed **on behalf of**
    them (option (a)); if no representative is on file, the beneficiary is kept with
    a reviewer flag. Emails are used only when present — never invented.
    """
    name = _beneficiary_name(attrs)
    if not name:
        return []
    email = _text(attrs.get("email")) or None
    address = _beneficiary_address(attrs)
    contingent = "possibly contingent — confirm" if _looks_contingent(attrs) else None

    if attrs.get("isMinor") or attrs.get("isMentallyIncapable"):
        status = "minor" if attrs.get("isMinor") else "incapable"
        rep_name = " ".join(
            _text(p)
            for p in (attrs.get("representativeFirstName"), attrs.get("representativeLastName"))
            if _text(p)
        ).strip()
        if rep_name:
            hint = "; ".join(x for x in (contingent,) if x) or None
            return [
                Recipient(
                    name=rep_name,
                    email=_text(attrs.get("representativeEmail")) or None,
                    address=_est_address(attrs.get("representativeAddress")),
                    on_behalf_of=name,
                    hint=hint,
                )
            ]
        # Guardian required but not recorded — keep the beneficiary, flag for staff.
        gap = f"{status} beneficiary — guardian/representative not on file"
        hint = "; ".join(x for x in (gap, contingent) if x)
        return [Recipient(name=name, email=email, address=address, hint=hint)]

    org = "organization — no personal email" if _is_org_beneficiary(attrs) and not email else None
    hint = "; ".join(x for x in (contingent, org) if x) or None
    return [Recipient(name=name, email=email, address=address, hint=hint)]


def _split_amount(txn: dict[str, Any]) -> tuple[Decimal | None, Decimal | None]:
    """Return ``(funds_out, funds_in)`` for a transaction.

    Prefers the explicit ``type`` (credit/debit); falls back to the sign of the
    amount when the type is missing or unrecognized.
    """
    amount = _money(txn.get("amount"))
    if amount is None:
        return None, None
    ttype = _text(txn.get("type")).lower()
    magnitude = abs(amount)
    if ttype in _CREDIT_TYPES:
        return None, magnitude
    if ttype in _DEBIT_TYPES:
        return magnitude, None
    # Unknown type: negative amount ⇒ out, positive ⇒ in.
    if amount < 0:
        return magnitude, None
    return None, magnitude


def _txn_date(txn: dict[str, Any]) -> date | None:
    raw = _text(txn.get("date") or txn.get("transaction_date"))
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _months_before(day: date, months: int) -> date:
    """The date ``months`` calendar months before ``day`` (clamped to month length)."""
    total = day.year * 12 + (day.month - 1) - months
    year, month0 = divmod(total, 12)
    month = month0 + 1
    # Clamp the day to the target month's length (e.g. Mar 31 - 1mo -> Feb 28/29).
    if month == 12:
        last = 31
    else:
        last = (date(year, month + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(day.day, last))


@dataclass
class _LedgerData:
    """Result of assembling a matter's trust ledger for the period."""

    rows: list[LedgerEntry]
    total_out: Decimal
    total_in: Decimal
    account_name: str  # the primary account, whose transactions form the ledger table
    account_type: str
    account_id: str | None
    other_accounts: list[str]  # other trust accounts with activity (not in the table)
    account_balances: dict[str, Decimal]  # per-account closing balance (name -> amount)
    account_last_txn: dict[str, date | None]  # per-account most recent txn date <= period end
    note: str | None  # reviewer note when multiple trust accounts are involved


@dataclass
class _AssetSummary:
    """Result of the "Assets Held" classification step (feeds the model/manifest)."""

    summary_rows: list[SummaryRow]
    cash_balance: Decimal  # cash-classified accounts only
    total_assets: Decimal | None  # None while the outside-counsel portfolio is pending
    breakdown: dict[str, Decimal]  # category -> subtotal
    unclassified: list[str]  # accounts shown as Cash but not confidently classified
    stale: list[tuple[str, str]]  # real-property (name, as-at ISO) older than the cutoff
    real_property_as_at: str | None  # representative real-property valuation date (ISO)


# --- the generator ----------------------------------------------------------


class ReportGenerator:
    """Builds a :class:`ReportModel` from Clio for a given matter + period."""

    # Field specs verified against live Clio. The matter->client expansion only
    # supports scalar contact fields (nested email_addresses/addresses 400), so the
    # mailing address is fetched with a separate get_contact call.
    _MATTER_FIELDS = (
        "id,display_number,description,"
        "client{id,name,first_name,last_name,primary_email_address},"
        "responsible_attorney{id,name,email},"
        "originating_attorney{id,name,email}"
    )
    _CONTACT_FIELDS = (
        "id,name,primary_email_address,primary_address{street,city,province,postal_code}"
    )
    # bank_transactions has no balance/reference/contact fields; direction is the
    # sign of `amount` (Clio types are asset/liability, not credit/debit), and the
    # counterparty is in `source`.
    _TXN_FIELDS = "id,date,type,description,amount,source,bank_account{id,name,type}"

    def __init__(
        self,
        clio: ClioLike,
        classifier: AccountClassifier = DEFAULT_CLASSIFIER,
        *,
        estateably: EstateablyLike | None = None,
    ) -> None:
        self._clio = clio
        self._classifier = classifier
        # Optional: address the letter to Estateably beneficiaries (Nicole 2026-08-06).
        # When None, the letter is addressed to the Clio client contact as before.
        self._estateably = estateably
        self._estate_index_cache: dict[str, str] | None = None

    def build(
        self, matter_id: int | str, period: ReportingPeriod, *, letter_date: date
    ) -> ReportModel:
        matter = self._clio.get_matter(matter_id, fields=self._MATTER_FIELDS)
        if not matter:
            raise ReportDataError(f"matter {matter_id} not found in Clio")

        matter_number = _text(matter.get("display_number")) or str(matter_id)
        trust_name = _text(matter.get("description")) or _text(
            (matter.get("client") or {}).get("name")
        )
        attorney = self._attorney(matter)
        recipients, recipient_note = self._recipients(matter)

        ledger_data = self._ledger(matter_id, period)

        assets = self._summary(
            ledger_data.account_balances, ledger_data.account_last_txn, period
        )

        source_refs = {
            "clio_matter_id": str(matter_id),
            "clio_matter_number": matter_number,
            "clio_bank_account_id": ledger_data.account_id,
            "ledger_row_count": len(ledger_data.rows),
            "other_trust_accounts": ledger_data.other_accounts,
            "trust_account_balances": {n: str(b) for n, b in ledger_data.account_balances.items()},
        }

        return ReportModel(
            matter_number=matter_number,
            trust_name=trust_name,
            attorney=attorney,
            recipients=recipients,
            period=period,
            letter_date=letter_date,
            account_name=ledger_data.account_name,
            account_type=ledger_data.account_type,
            ledger=ledger_data.rows,
            total_funds_out=ledger_data.total_out,
            total_funds_in=ledger_data.total_in,
            cash_balance=assets.cash_balance,
            summary_rows=assets.summary_rows,
            total_assets=assets.total_assets,
            investment_pending=True,
            asset_breakdown=assets.breakdown,
            unclassified_accounts=assets.unclassified,
            stale_valuations=assets.stale,
            real_property_as_at=assets.real_property_as_at,
            other_account_note=ledger_data.note,
            recipient_note=recipient_note,
            source_refs=source_refs,
        )

    # -- header pieces --------------------------------------------------------

    def _attorney(self, matter: dict[str, Any]) -> AttorneyContact:
        att = matter.get("responsible_attorney") or matter.get("originating_attorney") or {}
        return AttorneyContact(
            name=_text(att.get("name")) or "Aphrodite",
            email=att.get("email"),
            phone=None,  # phone/ext not on the Clio attorney record — see README open item
        )

    def _recipients(self, matter: dict[str, Any]) -> tuple[list[Recipient], str | None]:
        """Address the statement.

        When Estateably is wired, address the matter's beneficiaries (option (a):
        each beneficiary, and for a minor/incapable one the guardian/representative
        on their behalf). Falls back to the Clio client contact — with a reviewer
        note — when Estateably has no matching estate, no beneficiaries, or errors.
        """
        matter_number = _text(matter.get("display_number"))
        beneficiaries, note = self._beneficiary_recipients(matter_number)
        if beneficiaries is not None:
            return beneficiaries, note
        return self._client_recipients(matter), note

    def _client_recipients(self, matter: dict[str, Any]) -> list[Recipient]:
        """The matter's Clio client contact as a single recipient (the fallback)."""
        client = matter.get("client") or {}
        name = _text(client.get("name")) or " ".join(
            p for p in (client.get("first_name"), client.get("last_name")) if p
        )
        if not name:
            return []
        email = _first_email(client)
        # The mailing address isn't reachable through the matter->client expansion,
        # so fetch it from the contact directly (best-effort — a draft either way).
        address = None
        client_id = client.get("id")
        if client_id is not None:
            try:
                contact = self._clio.get_contact(client_id, fields=self._CONTACT_FIELDS)
                address = _format_address(contact)
                email = email or _first_email(contact)
            except Exception:  # noqa: BLE001 - address is optional; never fail the draft on it
                logger.warning(
                    "could not fetch contact address",
                    extra={"extra_fields": {"contact_id": str(client_id)}},
                )
        return [Recipient(name=name, email=email, address=address)]

    # -- beneficiary recipients (Estateably) ----------------------------------

    def _estate_index(self) -> dict[str, str]:
        """Matter number → Estateably estate id, built once per warm container.

        Deterministic by number (structured field, else the trailing number in the
        file name/title), avoiding the known Estateably wrong-case-by-name risk.
        """
        if self._estate_index_cache is not None:
            return self._estate_index_cache
        index: dict[str, str] = {}
        if self._estateably is not None:
            try:
                for est in self._estateably.list_estates():
                    number = _estate_matter_number(_attrs(est))
                    est_id = est.get("id")
                    if number and est_id and number not in index:
                        index[number] = str(est_id)
            except Exception:  # noqa: BLE001 - never fail a draft on Estateably being unavailable
                logger.warning("could not load Estateably estate index; using Clio recipients")
        self._estate_index_cache = index
        return index

    def _beneficiary_recipients(
        self, matter_number: str
    ) -> tuple[list[Recipient] | None, str | None]:
        """Beneficiary recipients for a matter, or ``(None, note)`` to fall back.

        Returns ``(None, None)`` when Estateably isn't wired (pure legacy behavior);
        ``(None, note)`` when it is wired but the estate/beneficiaries can't be used
        (so the caller falls back to the Clio client with an explanatory note); and
        ``(recipients, note)`` when beneficiaries were found.
        """
        if self._estateably is None:
            return None, None
        estate_id = self._estate_index().get(matter_number) if matter_number else None
        if not estate_id:
            return None, (
                "Estateably estate not matched for this matter — addressed to the Clio "
                "client; confirm the recipient list."
            )
        try:
            raw = self._estateably.list_beneficiaries(estate_id)
        except Exception:  # noqa: BLE001 - never fail a draft on an Estateably error
            logger.warning(
                "could not fetch beneficiaries",
                extra={"extra_fields": {"estate_id": estate_id}},
            )
            return None, (
                "Beneficiaries could not be retrieved from Estateably — addressed to the "
                "Clio client; confirm the recipient list."
            )
        recipients: list[Recipient] = []
        for item in raw:
            recipients.extend(_beneficiary_to_recipients(_attrs(item)))
        recipients = [r for r in recipients if r.name]
        if not recipients:
            return None, (
                "No beneficiaries on file in Estateably for this matter — addressed to the "
                "Clio client; confirm the recipient list."
            )
        note = (
            "Recipients are the beneficiaries on file in Estateably. Statements are for "
            "CURRENT INCOME beneficiaries only — confirm the list and remove any contingent "
            "or remainder beneficiaries before sending."
        )
        missing = sum(1 for r in recipients if not r.email)
        if missing:
            note += f" {missing} recipient(s) have no email on file (placeholder shown)."
        return recipients, note

    # -- trust ledger ---------------------------------------------------------

    def _ledger(self, matter_id: int | str, period: ReportingPeriod) -> _LedgerData:
        """Assemble the matter's trust ledger for the period.

        All of the matter's transactions are fetched (so running/opening balances
        are correct), then filtered to **trust** accounts — operating-account rows
        are the fee side of transfers and would double-count.

        A matter can span more than one trust account (HT keeps per-matter/-client
        sub-accounts, e.g. a main account + GIC accounts, or committeeship real
        property / investment accounts). Per-account closing balances and each
        account's most recent transaction date are returned so the summary step can
        classify them into the Assets Held breakdown; the detailed ledger table is the
        busiest ("primary") account, and a reviewer note lists the rest.
        """
        txns = self._clio.list_bank_transactions(matter_id=matter_id, fields=self._TXN_FIELDS)
        trust_txns = [
            t
            for t in txns
            if _text((t.get("bank_account") or {}).get("type")).lower() == _TRUST_ACCOUNT_TYPE
        ]
        if not trust_txns:
            logger.info("matter has no trust-account transactions; empty ledger")
            return _LedgerData(
                rows=[],
                total_out=Decimal("0"),
                total_in=Decimal("0"),
                account_name="Trust Account",
                account_type="Trust",
                account_id=None,
                other_accounts=[],
                account_balances={},
                account_last_txn={},
                note=None,
            )

        by_account: dict[Any, list[dict[str, Any]]] = {}
        for t in trust_txns:
            by_account.setdefault((t.get("bank_account") or {}).get("id"), []).append(t)

        # Closing balance + most-recent transaction date per account (matter-scoped,
        # as of period end) + names. The last-txn date is the real-property "as at"
        # valuation date (HT records a new assessment as a balance adjustment).
        account_balances: dict[str, Decimal] = {}
        account_last_txn: dict[str, date | None] = {}
        account_names: dict[Any, str] = {}
        for aid, ts in by_account.items():
            name = _text((ts[0].get("bank_account") or {}).get("name")) or "Trust Account"
            account_names[aid] = name
            bal = Decimal("0")
            last: date | None = None
            for t in ts:
                day = _txn_date(t)
                if day is not None and day <= period.end:
                    bal += _money(t.get("amount")) or Decimal("0")
                    if last is None or day > last:
                        last = day
            account_balances[name] = bal
            account_last_txn[name] = last

        # The primary account (most transactions) supplies the detailed table.
        primary_id = max(by_account, key=lambda k: len(by_account[k]))
        primary = by_account[primary_id][0].get("bank_account") or {}
        other_accounts = [account_names[aid] for aid in by_account if aid != primary_id]

        rows, total_out, total_in = self._ledger_rows(by_account[primary_id], period)

        note = None
        if len(by_account) > 1:
            parts = "; ".join(f"{n}: {money(b)}" for n, b in account_balances.items())
            note = (
                f"This matter holds assets across {len(by_account)} trust accounts; the "
                f"enclosed ledger details {account_names[primary_id]}, and the Assets Held "
                f"summary classifies each account by type. Per-account balances — {parts}."
            )

        return _LedgerData(
            rows=rows,
            total_out=total_out,
            total_in=total_in,
            account_name=account_names[primary_id],
            account_type=_text(primary.get("type")).title() or "Trust",
            account_id=str(primary_id) if primary_id is not None else None,
            other_accounts=other_accounts,
            account_balances=account_balances,
            account_last_txn=account_last_txn,
            note=note,
        )

    def _ledger_rows(
        self, account_txns: list[dict[str, Any]], period: ReportingPeriod
    ) -> tuple[list[LedgerEntry], Decimal, Decimal]:
        """Build the in-period ledger rows + totals for one account (running balance)."""
        dated = [(d, t) for t in account_txns if (d := _txn_date(t)) is not None]
        dated.sort(key=lambda pair: (pair[0], str(pair[1].get("id", ""))))

        running = Decimal("0")
        rows: list[LedgerEntry] = []
        total_out = Decimal("0")
        total_in = Decimal("0")
        for day, txn in dated:
            funds_out, funds_in = _split_amount(txn)
            running += (funds_in or Decimal("0")) - (funds_out or Decimal("0"))
            if period.contains(day):
                rows.append(
                    LedgerEntry(
                        date=day.isoformat(),
                        source_dest=_text(txn.get("source")),
                        reference=_text(txn.get("description")),
                        payer_payee="",  # counterparty is in source_dest; kept for template parity
                        funds_out=funds_out,
                        funds_in=funds_in,
                        balance=running,
                    )
                )
                total_out += funds_out or Decimal("0")
                total_in += funds_in or Decimal("0")
        return rows, total_out, total_in

    # -- cover-letter "Assets Held" summary -----------------------------------

    def _summary(
        self,
        account_balances: dict[str, Decimal],
        account_last_txn: dict[str, date | None],
        period: ReportingPeriod,
    ) -> _AssetSummary:
        """Build the itemized "Assets Held" breakdown (Nicole's Decisions A/D/E).

        Each trust account is classified (Cash / Real Property / Investments / GICs)
        and its closing balance summed into that category. Real property carries an
        "as at" valuation date (its most recent adjustment) and is flagged when >15
        months old. The outside-counsel investment portfolio is a highlighted staff
        fill-in (Decision B), so Total Assets stays provisional. Assets only — no
        liabilities.
        """
        breakdown: dict[str, Decimal] = {
            CATEGORY_CASH: Decimal("0"),
            CATEGORY_REAL_PROPERTY: Decimal("0"),
            CATEGORY_INVESTMENTS: Decimal("0"),
            CATEGORY_GICS: Decimal("0"),
        }
        names: dict[str, list[str]] = {c: [] for c in breakdown}
        unclassified: list[str] = []
        for name, balance in account_balances.items():
            category, confident = self._classifier.classify(name)
            breakdown[category] += balance
            names[category].append(name)
            if not confident:
                unclassified.append(name)

        # Real-property valuation date + staleness (Decision D).
        cutoff = _months_before(period.end, _STALE_VALUATION_MONTHS)
        rp_dates = [d for n in names[CATEGORY_REAL_PROPERTY] if (d := account_last_txn.get(n))]
        real_property_as_at = max(rp_dates).isoformat() if rp_dates else None
        stale: list[tuple[str, str]] = []
        for name in names[CATEGORY_REAL_PROPERTY]:
            day = account_last_txn.get(name)
            if day is None or day < cutoff:
                stale.append((name, day.isoformat() if day else "unknown"))

        rows: list[SummaryRow] = [
            SummaryRow(description="Cash Accounts", amount=breakdown[CATEGORY_CASH])
        ]
        if names[CATEGORY_REAL_PROPERTY]:
            as_at = long_date(max(rp_dates)) if rp_dates else "[date to be confirmed]"
            rows.append(
                SummaryRow(
                    description="Real Property",
                    amount=breakdown[CATEGORY_REAL_PROPERTY],
                    detail=f"as at {as_at}, per most recent assessment/appraisal",
                )
            )
        if names[CATEGORY_INVESTMENTS]:
            rows.append(
                SummaryRow(
                    description="Investments",
                    amount=breakdown[CATEGORY_INVESTMENTS],
                    detail="; ".join(names[CATEGORY_INVESTMENTS]),
                )
            )
        if names[CATEGORY_GICS]:
            rows.append(
                SummaryRow(
                    description="GICs",
                    amount=breakdown[CATEGORY_GICS],
                    detail="; ".join(names[CATEGORY_GICS]),
                )
            )
        # Outside-counsel investment portfolio: staff fill-in during review (Decision B).
        # Always present so a portfolio is never missed; Total stays provisional.
        rows.append(
            SummaryRow(
                description=(
                    "Investment Portfolio - outside investment counsel "
                    "(enter total from attached statement)"
                ),
                amount=None,
                provisional=True,
                fill_in=True,
            )
        )
        rows.append(SummaryRow(description="Total Assets", amount=None, provisional=True))

        return _AssetSummary(
            summary_rows=rows,
            cash_balance=breakdown[CATEGORY_CASH],
            total_assets=None,
            breakdown=breakdown,
            unclassified=unclassified,
            stale=stale,
            real_property_as_at=real_property_as_at,
        )
