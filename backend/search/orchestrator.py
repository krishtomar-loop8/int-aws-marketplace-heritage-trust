"""Fan-out orchestration → compact, provenance-tagged matter dossier.

Given a resolved matter (per-system record ids from :mod:`backend.search.resolve`),
this reads the inputs each output section needs from the source systems and
assembles a **matter dossier**: a compact, structured intermediate that the
synthesis step (WS-D) turns into the source-cited status summary.

Design choices (accuracy-first, cost-aware):
- **Parallel across sources, sequential within one.** Each connector is touched
  by exactly one worker thread, so the connectors' (non-thread-safe) rate
  limiters are never shared across threads while we still get the latency win of
  hitting Estateably/Clio concurrently.
- **Compact, not raw.** High-volume series (tasks, notes, transactions) are
  bounded to the most recent N and flagged ``truncated`` so synthesis can say
  "reviewed to [date]"; documents contribute names/metadata, not binaries. This
  keeps prompt tokens (the real cost) down *and* improves accuracy (less noise).
- **Partial results are fine.** A failing pull is recorded in ``errors`` and
  skipped; one dead source never sinks the whole dossier.

Slice 1 reads Estateably + Clio. QuickBooks reconciliation and Graph
(Outlook/SharePoint) join in Slice 2 (QuickBooks needs a matter→customer mapping;
see ``_collect_quickbooks``).
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from backend.search.deeplinks import (
    clio_matter_url,
    estateably_estate_url,
    estateably_is_trust,
    quickbooks_customer_url,
)
from backend.search.resolve import MatterCandidate, _normalize
from backend.search.scoping import belongs_to_matter, is_excluded
from services.http import HttpError
from services.logging import get_logger

logger = get_logger(__name__)

# 6-digit Aphrodite matter number (e.g. 190024), as it appears in a candidate
# name/label and in SharePoint folder paths (.../C/<client>/<matter>/...).
_MATTER_NO_RE = re.compile(r"\b\d{6}\b")

# Concurrency for the per-mailbox Outlook $search fan-out (pure I/O — the fan-out
# long pole). Graph throttling (429) is retried with Retry-After in services.http.
_OUTLOOK_WORKERS = 16

# Correspondence content budgets. The ~255-char bodyPreview lost email substance
# (a lawsuit-announcing email read as routine — client feedback round 2), so the
# selected messages' full uniqueBody text is hydrated into the dossier. Budgets
# bound the prompt on huge matters: newest emails get the full per-email budget;
# once the global budget is spent, older emails degrade to the floor instead of
# losing their body entirely. Truncation is always marked, never silent.
_BODY_CHAR_BUDGET = 1_500
_BODY_CHAR_FLOOR = 600
# Forwarded emails carry their substance in the forwarded/quoted portion, which
# uniqueBody strips — so they are hydrated from the full body and given a larger
# excerpt so the forwarded content (not just the sender's cover note) survives.
_FORWARD_BODY_BUDGET = 3_000
_CORRESPONDENCE_CHAR_BUDGET = 75_000
_TRUNCATION_MARKER = " …[truncated — open in Outlook]"
# One conversation may take at most this many correspondence slots, so a busy
# thread cannot evict single-message threads (an instructions email, a bill).
_CONVERSATION_CAP = 8

# All dossier-visible dates are Aphrodite local time — Graph returns UTC
# ("Z"), which shifted late-evening Pacific emails to the next calendar day.
_PACIFIC = ZoneInfo("America/Vancouver")

# TTL cache for query-independent, org-wide pulls (the mailbox roster, firm-wide
# Clio bank accounts). Shared across warm-container invocations so repeated
# searches skip re-fetching; the short TTL keeps it reasonably fresh. The roster
# TTL stays well under the Graph token's ~1h life, so a roster cache hit always
# implies a still-valid token (no refresh race in the parallel fan-out).
_SHARED_CACHE_TTL_SECONDS = 300.0
_shared_cache: dict[str, tuple[float, Any]] = {}
_shared_cache_lock = threading.Lock()


def _cached(
    key: str, producer: Callable[[], Any], *, ttl: float = _SHARED_CACHE_TTL_SECONDS
) -> Any:
    """Return a fresh cached value for ``key``, else produce and cache it.

    The producer (a network call) runs OUTSIDE the lock, so a rare concurrent miss
    double-fetches (benign) instead of serializing every caller behind the lock.
    A producer that raises is not cached (the caller's ``sl.safe`` records it).
    """
    hit = _shared_cache.get(key)
    if hit is not None and time.monotonic() - hit[0] < ttl:
        return hit[1]
    value = producer()
    with _shared_cache_lock:
        _shared_cache[key] = (time.monotonic(), value)
    return value


@dataclass(frozen=True)
class DossierLimits:
    """Per-section caps that bound prompt size (most-recent-N)."""

    # Caps favor completeness over prompt size — accuracy matters more than
    # latency for this agent (missing a KYC/Tax doc or an email is worse than a
    # slower answer). Kept high enough to cover a full file, with a marker when
    # exceeded so the model knows the list was capped.
    tasks: int = 60
    notes: int = 40
    activities: int = 40
    transactions: int = 60
    calendar: int = 40
    documents: int = 200
    parties: int = 80
    # SharePoint documents retrieved from the Bedrock Knowledge Base (top-k chunks).
    sharepoint_docs: int = 40
    # Outlook correspondence fan-out (all-mailbox subject search). The mailbox
    # list is enumerated live from the directory on every search, so new staff are
    # picked up automatically; this cap is a safety ceiling, set well above HT's
    # headcount so realistic growth is covered — and any overflow is surfaced (not
    # silently dropped) in the fan-out's reviewed note.
    mailboxes: int = 150
    # Per-mailbox search page. Graph mail $search returns at most this many hits
    # per mailbox, ranked by Graph *relevance* (not recency) and NOT paginated, so
    # a genuinely on-matter message that ranks past this cut is dropped before the
    # fan-out ever sees it. At 20 a matter-heavy mailbox (100+ hits) silently lost
    # recent matter threads (a real Bell death-certificate thread ranked #24–38);
    # 50 recovers them while keeping the per-mailbox payload bounded.
    messages_per_mailbox: int = 50
    correspondence: int = 80


DEFAULT_LIMITS = DossierLimits()


@dataclass(frozen=True)
class DossierItem:
    """One provenance-tagged datum destined for a synthesis citation."""

    system: str  # "estateably" | "clio" | "quickbooks"
    record_id: str
    summary: str
    date: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class DossierSection:
    key: str
    items: list[DossierItem] = field(default_factory=list)
    truncated: bool = False


@dataclass
class MatterDossier:
    matter_name: str
    matter_type: str | None
    refs: list[Any] = field(default_factory=list)
    sections: dict[str, DossierSection] = field(default_factory=dict)
    sources_reviewed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Per-source fan-out wall-time (ms), for latency instrumentation. Not serialized
    # into the synthesis prompt / API — surfaced only in the search.completed audit.
    timings: dict[str, float] = field(default_factory=dict)

    def section(self, key: str) -> DossierSection:
        return self.sections.setdefault(key, DossierSection(key=key))

    def as_dict(self) -> dict[str, Any]:
        """JSON-serializable view for the synthesis prompt / API response."""
        return {
            "matter_name": self.matter_name,
            "matter_type": self.matter_type,
            "sections": {
                k: {
                    "truncated": s.truncated,
                    "items": [
                        {
                            "system": i.system,
                            "record_id": i.record_id,
                            "date": i.date,
                            "summary": i.summary,
                            **({"data": i.data} if i.data else {}),
                        }
                        for i in s.items
                    ],
                }
                for k, s in self.sections.items()
            },
            "sources_reviewed": self.sources_reviewed,
            "errors": self.errors,
        }


# -- helpers ----------------------------------------------------------------


def _first(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        v = d.get(k)
        if v:
            return v
    return None


def _attrs(item: dict[str, Any]) -> dict[str, Any]:
    """Estateably JSON:API resources nest fields under ``attributes``."""
    a = item.get("attributes")
    return a if isinstance(a, dict) else item


def _searchable_mailbox(u: dict[str, Any]) -> bool:
    """A mail-enabled staff account whose inbox app-only ``$search`` can read.

    Include any enabled, non-guest account that has a mailbox address — this
    deliberately keeps **unlicensed** mailboxes (shared inboxes like ``Estates@``
    and staff on non-mailbox licenses), which at HT hold real correspondence: an
    ``assignedLicenses`` check was found to wrongly drop ~9 working mailboxes.
    Disabled accounts (offboarded) and external guests are excluded; the rare
    account with no REST mailbox is caught at search time as a quiet skip.
    """
    if not u.get("mail"):
        return False
    if not u.get("accountEnabled", True):
        return False
    return u.get("userType") != "Guest"


def _longest_token(name: str) -> str:
    """The most distinctive (longest) significant token of a name — a surname is
    usually longest, and the best single term for a QuickBooks LIKE lookup."""
    tokens = _normalize(name)
    return max(tokens, key=len) if tokens else ""


def _best_customer_match(name: str, customers: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the customer whose display name shares the most tokens with ``name``."""
    want = _normalize(name)
    best: dict[str, Any] | None = None
    best_overlap = 0
    for c in customers:
        if not isinstance(c, dict):
            continue
        disp = c.get("DisplayName") or c.get("CompanyName") or ""
        overlap = len(want & _normalize(str(disp)))
        if overlap > best_overlap:
            best, best_overlap = c, overlap
    return best if best_overlap > 0 else None


def _match_customer(
    customers: list[dict[str, Any]], qb_customer_id: str | None, matter_name: str
) -> dict[str, Any] | None:
    """Pick the QuickBooks customer for this matter.

    Prefer an exact match on the Clio client-contact id stamped into the DisplayName
    ("(Clio <id>)") — stable across Clio renames. Fall back to fuzzy name matching
    only when the matter has no client-contact id.
    """
    if qb_customer_id:
        pat = re.compile(rf"\(\s*clio\s*{re.escape(str(qb_customer_id))}\s*\)", re.IGNORECASE)
        for c in customers:
            if not isinstance(c, dict):
                continue
            if pat.search(c.get("DisplayName") or "") or pat.search(
                c.get("FullyQualifiedName") or ""
            ):
                return c
        return None
    return _best_customer_match(matter_name, customers)


def _amount(value: Any) -> float:
    """Coerce a money field (number, or a string like '$1,234.50') to a float."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^\d.\-]", "", str(value))
    try:
        return float(cleaned) if cleaned not in ("", "-", ".", "-.") else 0.0
    except ValueError:
        return 0.0


# A Clio↔QuickBooks total within this many dollars is treated as reconciled.
_RECONCILE_TOLERANCE = 0.01
# Clio bill states that never issue to QuickBooks — excluded from reconciliation
# (a deleted duplicate of a bill number would otherwise double-count the invoice).
_INACTIVE_BILL_STATES = {"draft", "void", "deleted"}


def _clio_bill_number(docnumber: str | None) -> str | None:
    """The Clio bill number a QuickBooks invoice maps to.

    Clio's QuickBooks sync stamps the bill number into the invoice DocNumber as
    "clio-<number>". A QuickBooks-native invoice (no Clio bill) uses "QB-<number>"
    (or anything else), for which this returns None.
    """
    m = re.match(r"(?i)^clio-(.+)$", (docnumber or "").strip())
    return m.group(1).strip() if m else None


def _reconcile(dossier: MatterDossier, metrics: dict[str, Any]) -> None:
    """Reconcile Clio bills against QuickBooks invoices, matched by invoice number.

    Clio's QuickBooks sync issues each Clio bill as a QuickBooks invoice numbered
    "clio-<bill number>"; a QuickBooks-native invoice is "QB-<number>". Matching on
    that number (deduping Clio's deleted duplicates upstream, comparing invoiced =
    TotalAmt and paid = TotalAmt − Balance) reconciles like-for-like, then flags the
    exceptions HT's finance team cares about: QuickBooks-only invoices and Clio bills
    not synced to QuickBooks. Computed deterministically so the model reports the
    result rather than doing the arithmetic. Trust balances stay Clio-only (not here).
    """
    clio = metrics.get("clio_financials")
    qb = metrics.get("qb_financials")
    if not clio or not qb:
        return  # need both sides matched to reconcile
    bills = {str(b["number"]): b for b in clio.get("bills", []) if b.get("number")}
    matched: set[str] = set()
    m_clio_inv = m_clio_paid = m_qb_inv = m_qb_paid = 0.0
    qb_only: list[str] = []
    for inv in qb.get("invoices", []):
        number = inv.get("clio_number")
        if number and number in bills:
            matched.add(number)
            m_qb_inv += _amount(inv.get("invoiced"))
            m_qb_paid += _amount(inv.get("paid"))
        else:
            qb_only.append(f"{inv.get('docnumber') or '?'} (${_amount(inv.get('invoiced')):,.2f})")
    for number in matched:
        m_clio_inv += _amount(bills[number].get("total"))
        m_clio_paid += _amount(bills[number].get("paid"))
    clio_only = sorted(set(bills) - matched)

    parts = []
    for label, cv, qv in (("Invoices", m_clio_inv, m_qb_inv), ("Payments", m_clio_paid, m_qb_paid)):
        diff = round(cv - qv, 2)
        verdict = (
            "agree"
            if abs(diff) <= _RECONCILE_TOLERANCE
            else f"DISCREPANCY ${abs(diff):,.2f} (review)"
        )
        parts.append(f"{label}: Clio ${cv:,.2f} vs QuickBooks ${qv:,.2f} — {verdict}")
    summary = "Clio↔QuickBooks reconciliation (matched by invoice number) — " + "; ".join(parts)
    if qb_only:
        summary += f". QuickBooks-only invoice(s), no Clio bill: {'; '.join(qb_only)} — review"
    if clio_only:
        summary += f". Clio bill(s) not found in QuickBooks: {', '.join(clio_only)} — review"
    matters = int(clio.get("matters") or 1)
    if matters > 1:
        summary += (
            f" (client-level: this client's {matters} matters all bill under one "
            "QuickBooks customer, so invoices are matched across the client's matters)"
        )
    dossier.section("financials").items.append(DossierItem("reconciliation", "clio-qb", summary))


def _estateably_component_sums(
    accounts: list[dict[str, Any]], inventory: list[dict[str, Any]]
) -> dict[str, Any]:
    """Sum Estateably's financial component rows for the internal check.

    Inventory rows use the same value keys the display summarizer reads; a row
    with no value (or a non-numeric placeholder like "TBD") counts as *unvalued*
    — a partially-valued list can't prove a discrepancy. Account balances live in
    ``meta.balance.value``. The inventory's own "bank accounts" category line is
    summed separately: it overlaps the account records, so the check needs it to
    test the with/without-overlap compositions.
    """
    inv_total = inv_bank_total = acct_total = 0.0
    inv_unvalued = 0
    inv_items = 0
    for row in inventory:
        if not isinstance(row, dict):
            continue
        a = _attrs(row)
        inv_items += 1
        raw = next((a[k] for k in ("value", "deathValue", "amount") if a.get(k) is not None), None)
        if raw is None or (isinstance(raw, str) and not re.search(r"\d", raw)):
            inv_unvalued += 1
            continue
        amount = _amount(raw)
        inv_total += amount
        label = f"{a.get('category') or ''} {a.get('inventoryType') or ''}".lower()
        if "bank" in label:
            inv_bank_total += amount
    acct_unvalued = 0
    acct_items = 0
    for row in accounts:
        if not isinstance(row, dict):
            continue
        acct_items += 1
        meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
        balance = meta.get("balance")
        value = balance.get("value") if isinstance(balance, dict) else None
        if value is None:
            acct_unvalued += 1
            continue
        acct_total += _amount(value)
    return {
        "inventory_total": round(inv_total, 2),
        "inventory_bank_total": round(inv_bank_total, 2),
        "inventory_items": inv_items,
        "inventory_unvalued": inv_unvalued,
        "accounts_total": round(acct_total, 2),
        "accounts_items": acct_items,
        "accounts_unvalued": acct_unvalued,
    }


def _estateably_self_check(dossier: MatterDossier, metrics: dict[str, Any]) -> None:
    """Flag when Estateably's own components don't add up to its stated total.

    The client saw a $1.14M component-vs-total gap presented without comment —
    the only deterministic reconciliation was Clio↔QuickBooks. Estateably's
    ``currentInventory`` total has no single documented composition (account
    records overlap the inventory's "Bank Accounts" category line), so rather
    than assume one and false-positive on every file, the check accepts ANY
    plausible composition of the components; a DISCREPANCY is flagged only when
    none reconciles within tolerance (0.5% or $100, whichever is larger).
    Computed in code so the model reports the result, never the arithmetic.
    """
    est = metrics.get("estateably_financials")
    if not est:
        return
    stated = _amount(est.get("stated_inventory"))
    n_items = int(est.get("inventory_items") or 0) + int(est.get("accounts_items") or 0)
    if stated <= 0 or n_items == 0:
        return  # nothing stated (or an unpopulated file) — nothing to check
    inv = float(est.get("inventory_total") or 0.0)
    acct = float(est.get("accounts_total") or 0.0)
    bank = float(est.get("inventory_bank_total") or 0.0)
    unvalued = int(est.get("inventory_unvalued") or 0) + int(est.get("accounts_unvalued") or 0)
    section = dossier.section("financials")
    if unvalued:
        section.items.append(
            DossierItem(
                "reconciliation",
                "estateably-internal",
                (
                    f"Estateably internal check: not fully checkable — {unvalued} "
                    f"component record(s) carry no value (inventory items ${inv:,.2f}, "
                    f"account balances ${acct:,.2f}, stated current inventory "
                    f"${stated:,.2f})"
                ),
            )
        )
        return
    compositions: dict[str, float] = {
        "inventory line items": inv,
        "account balances": acct,
        "inventory + accounts": inv + acct,
    }
    if bank:
        compositions["inventory + accounts, less the bank-accounts inventory line"] = (
            inv + acct - bank
        )
    tolerance = max(0.005 * stated, 100.0)
    name, total = min(compositions.items(), key=lambda kv: abs(kv[1] - stated))
    diff = round(abs(total - stated), 2)
    if diff <= tolerance:
        summary = (
            f"Estateably internal check: stated current inventory ${stated:,.2f} "
            f"reconciles with {name} (${total:,.2f}) — agree"
        )
    else:
        summary = (
            f"Estateably internal check: stated current inventory ${stated:,.2f} vs "
            f"closest component sum ${total:,.2f} ({name}) — DISCREPANCY ${diff:,.2f} "
            f"(review); inventory items ${inv:,.2f}, account balances ${acct:,.2f}"
        )
    section.items.append(DossierItem("reconciliation", "estateably-internal", summary))


def _parse_iso_date(value: Any) -> date | None:
    """Parse an ISO date (accepts a datetime prefix); None on anything unparseable."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _add_months(d: date, months: int) -> date:
    """d shifted forward by ``months``, clamping the day to the target month's end
    (e.g. Dec 31 + 6 months → Jun 30)."""
    m = d.month - 1 + months
    year, month = d.year + m // 12, m % 12 + 1
    first_next = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last_day = (first_next - timedelta(days=1)).day
    return date(year, month, min(d.day, last_day))


def _compute_tax_deadlines(dossier: MatterDossier, metrics: dict[str, Any]) -> None:
    """Compute the terminal-T1 filing deadline deterministically from the date of
    death, so the model reports it rather than doing the arithmetic (which produced
    wrong dates, e.g. a 2026-01-15 death yielding "July 15, 2027").

    CRA rule = the later of April 30 of the year after death, or six months after
    the date of death (the six-month arm binds only for Nov/Dec deaths). If
    Estateably separately records a terminal-T1 deadline that differs, both are
    surfaced as a discrepancy to review. T3/estate-return deadlines are left to the
    dossier/model — they depend on the (unknown) graduated-rate-estate year-end.
    """
    dates = metrics.get("estate_dates") or {}
    dod = _parse_iso_date(dates.get("date_of_death"))
    if dod is None:
        return
    deadline = max(date(dod.year + 1, 4, 30), _add_months(dod, 6))
    summary = (
        f"Terminal T1 ({dod.year}) personal return — filing deadline "
        f"{deadline.isoformat()}, computed from the date of death "
        f"({dod.isoformat()}) per CRA (later of April 30 the following year, or "
        f"six months after death)"
    )
    stated = _parse_iso_date(dates.get("estateably_terminal_t1"))
    if stated and stated != deadline:
        summary += (
            f". NOTE: Estateably records {stated.isoformat()} — DISCREPANCY to "
            f"review; the computed date governs unless a filing extension applies"
        )
    dossier.section("deadlines").items.append(
        DossierItem("computed", "terminal-t1", summary)
    )


# -- PII masking ------------------------------------------------------------
# Client identifiers must never reach the model or the output in full. Mask
# social-insurance / tax-id numbers entirely and bank/account numbers to the last
# 4 digits, recursively, before a source record's raw attributes enter the dossier.
_SIN_KEYS = frozenset(
    {"sin", "sinnumber", "socialinsurancenumber", "tin", "tinnumber", "taxidnumber"}
)
# Account-number shapes in free text. Ordered from most to least specific:
# a 6-digit number with a short letter suffix ("851620-T" — RRIF/policy style);
# digit groups joined by -, space, or . whose digits total >=9 ("00960209844-0010");
# a bare run of >=7 digits (optionally the integer part of a money literal, which
# must stay readable — financial figures are load-bearing in dossiers).
_ACCT_SUFFIX_RE = re.compile(r"\b(\d{6})-([A-Za-z]{1,3})\b")
# a LETTER-prefixed identifier with grouped digits ("F416, 280-6" — insurance/
# contract-number style): 1-3 letters glued to >=2 digits, then >=1 further
# separator+digit group. The glued letter prefix distinguishes it from money and
# ISO dates (no letter), and the required trailing group spares bare file refs
# ("P261759", court action "S224727") which have no separator+digits after them.
_LETTER_ACCT_RE = re.compile(r"\b[A-Za-z]{1,3}\d{2,}(?:[,\s.\-]\s*\d+)+\b")
_SEP_GROUPS_RE = re.compile(r"\b\d+(?:[- .]\d+)+\b")
_LONG_RUN_RE = re.compile(r"(?<!\d)(\d{7,})(\.\d{2}(?!\d))?")


def _mask_last4(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value))
    return f"***{digits[-4:]}" if len(digits) >= 4 else "***"


def _mask_free_text(text: str) -> str:
    """Mask account-number-shaped identifiers in free text (document titles, email
    previews, note/comment text) to their last 4 digits — including letter-suffixed
    ("851620-T") and letter-prefixed, grouped contract numbers ("F416, 280-6").

    Deliberately survives: 6-digit matter numbers (260008 — scoping/citations),
    letter-prefixed file/court refs with no trailing digit group (P261759,
    S224727), 5-digit invoice numbers (10776 — reconciliation), ISO dates, and
    money figures (comma-grouped or 2-decimal literals like 18329212.09).
    """

    def _suffix(m: re.Match[str]) -> str:
        return f"***{m.group(1)[-4:]}-{m.group(2)}"

    def _letter_acct(m: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        return f"***{digits[-4:]}" if len(digits) >= 5 else m.group(0)

    def _groups(m: re.Match[str]) -> str:
        s = m.group(0)
        digits = re.sub(r"\D", "", s)
        if len(digits) < 9:
            return s  # short groupings ("5881 85", ISO dates) stay readable
        if re.match(r"\d{4}-\d{2}-\d{2}", s):
            return s  # date / datetime prefix
        if re.search(r"\.\d{2}$", s):
            return s  # money-like tail ("19 472 522.59")
        return f"***{digits[-4:]}"

    def _run(m: re.Match[str]) -> str:
        if m.group(2):  # integer part of a 2-decimal money literal
            return m.group(0)
        return f"***{m.group(1)[-4:]}"

    text = _ACCT_SUFFIX_RE.sub(_suffix, text)
    text = _LETTER_ACCT_RE.sub(_letter_acct, text)
    text = _SEP_GROUPS_RE.sub(_groups, text)
    return _LONG_RUN_RE.sub(_run, text)


def _estateably_account_summary(row: dict[str, Any]) -> str:
    """Label + real balance for an Estateably account. The balance lives in
    ``meta.balance.value`` (a sibling of ``attributes``), so this reads the full
    JSON:API row, not just its attributes; the account number is masked."""
    a = _attrs(row)
    meta = row.get("meta") if isinstance(row, dict) else None
    meta = meta if isinstance(meta, dict) else {}
    acct_type = _first(a, "accountType") or "account"
    who = _first(a, "institution", "accountNickname", "description")
    label = _mask_free_text(f"{acct_type} — {who}" if who else str(acct_type))
    if a.get("number"):
        label += f" (#{_mask_last4(a['number'])})"
    balance = meta.get("balance")
    value = balance.get("value") if isinstance(balance, dict) else None
    return f"{label}: balance {value if value is not None else '?'}"


def _estateably_inventory_summary(a: dict[str, Any]) -> str:
    """Label an Estateably inventory item: type + asset/liability + value, plus a
    Capital/Income tag where the record carries one (``fields.transactionMoneyType``)."""
    label = _mask_free_text(
        str(_first(a, "inventoryType", "name", "type", "description") or "item")
    )
    value = next((a[k] for k in ("value", "deathValue", "amount") if a.get(k) is not None), "")
    line = label
    if a.get("category"):
        line += f" [{a['category']}]"
    line += f": {value}"
    fields = a.get("fields")
    money = fields.get("transactionMoneyType") if isinstance(fields, dict) else None
    if money:
        line += f" ({money})"
    return line


def _estate_summary_line(a: dict[str, Any]) -> str:
    """The estate-summary line: stage plus the named estate-value / residual figures
    (labeled) so the model sees real numbers, not just a raw attribute blob."""
    stage = _first(a, "stage", "status", "phase") or "unknown"
    parts = [f"stage: {stage}"]
    for label, key in (
        ("estate value", "estateValue"),
        ("residual", "residual"),
        ("current inventory", "currentInventory"),
        ("tax payable", "taxPayable"),
    ):
        v = a.get(key)
        if v not in (None, ""):
            parts.append(f"{label}: {v}")
    return "; ".join(parts)


def _redact_pii(value: Any) -> Any:
    """Recursively mask SIN/TIN (fully) and account numbers (last 4) in a source
    record's attributes before they are dumped into the dossier/prompt."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key = "".join(ch for ch in str(k).lower() if ch.isalnum())
            if key in _SIN_KEYS:
                out[k] = "***"
            elif "accountnumber" in key or "acctnumber" in key or key == "accountno":
                out[k] = _mask_last4(v) if v not in (None, "") else v
            else:
                out[k] = _redact_pii(v)
        return out
    if isinstance(value, list):
        return [_redact_pii(v) for v in value]
    return value


def _matter_scope_keys(
    candidate: MatterCandidate, term: str
) -> tuple[frozenset[str], frozenset[str]]:
    """Identifiers for scoping SharePoint retrieval to this matter: the 6-digit
    matter number(s) and the significant client-name tokens (>=4 chars), drawn from
    the candidate name, its ref labels, and the correspondence term."""
    text = " ".join([candidate.name, term, *(r.label for r in candidate.refs)])
    numbers = frozenset(_MATTER_NO_RE.findall(text))
    tokens = frozenset(t for t in _normalize(f"{candidate.name} {term}") if len(t) >= 4)
    return numbers, tokens


# -- Outlook helpers ---------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")

# Email domains whose correspondence is development/test noise, not part of a
# client matter — excluded from the dossier so it can't muddy the synthesis. The
# dev team (@loop8.io) is the only such domain during the POC; extend as needed.
_EXCLUDED_CORRESPONDENT_DOMAINS = frozenset({"loop8.io"})


def _message_correspondents(message: dict[str, Any]) -> set[str]:
    """Every email address on a message — sender plus To/Cc recipients — lowercased."""
    addrs: set[str] = set()
    frm = ((message.get("from") or {}).get("emailAddress") or {}).get("address")
    if frm:
        addrs.add(str(frm).lower())
    for key in ("toRecipients", "ccRecipients"):
        for r in message.get(key) or []:
            a = ((r or {}).get("emailAddress") or {}).get("address")
            if a:
                addrs.add(str(a).lower())
    return addrs


def _is_excluded_correspondent(message: dict[str, Any]) -> bool:
    """True if any participant (from/to/cc) is on an excluded domain (e.g. the
    dev team's @loop8.io) — matches the domain itself or any subdomain."""
    for addr in _message_correspondents(message):
        domain = addr.rpartition("@")[2]
        if domain and any(
            domain == d or domain.endswith("." + d) for d in _EXCLUDED_CORRESPONDENT_DOMAINS
        ):
            return True
    return False


def _sender_label(message: dict[str, Any]) -> str:
    """Human sender for an Outlook message: display name preferred, SMTP address
    as fallback. Internal senders sometimes carry a legacy Exchange X.500 DN in
    ``address`` ("/O=EXCHANGELABS/OU=..."), which is meaningless to staff and the
    model — it is never rendered."""
    email = (message.get("from") or {}).get("emailAddress") or {}
    name = str(email.get("name") or "").strip()
    addr = str(email.get("address") or "").strip()
    if addr.lower().startswith("/o="):
        addr = ""
    if name and addr and name.lower() != addr.lower():
        return f"{name} <{addr}>"
    return name or addr or "(internal sender)"


def _to_pacific(iso_ts: str | None) -> str | None:
    """ISO datetime (Graph returns UTC "Z") → ISO in America/Vancouver.

    Bare dates (no time part) pass through unchanged; naive datetimes are treated
    as UTC. Unparseable values pass through untouched.
    """
    if not iso_ts or "T" not in str(iso_ts):
        return iso_ts
    try:
        dt = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
    except ValueError:
        return iso_ts
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(_PACIFIC).isoformat(timespec="seconds")


def _pacific_human(iso_ts: str | None) -> str:
    """Short human Pacific rendering for reviewed-to notes ("2026-07-29 9:38 pm")."""
    iso = _to_pacific(iso_ts)
    if not iso:
        return "unknown"
    try:
        dt = datetime.fromisoformat(str(iso))
    except ValueError:
        return str(iso)
    if (dt.hour, dt.minute, dt.second) == (0, 0, 0):
        return f"{dt:%Y-%m-%d}"
    hour = dt.strftime("%I").lstrip("0") or "12"
    return f"{dt:%Y-%m-%d} {hour}:{dt:%M} {dt.strftime('%p').lower()}"


def _body_text(content: str) -> str:
    """Collapse a message body to clean single-line text. Bodies are requested as
    plain text (Prefer: outlook.body-content-type="text"); this defensively strips
    any residual HTML tags/entities and collapses whitespace."""
    return " ".join(html.unescape(_TAG_RE.sub(" ", content)).split())


_FORWARD_SUBJECT_RE = re.compile(r"^\s*(fw|fwd)\s*:", re.IGNORECASE)


def _is_forwarded_subject(subject: str) -> bool:
    """True if a subject marks a forward ("FW:"/"Fwd:"). A forward's substance is
    the forwarded/quoted content, which ``uniqueBody`` excludes — so these are
    hydrated from the full body instead (a forwarded email announcing property
    issues was reaching the model as only the sender's one-line cover note)."""
    return bool(_FORWARD_SUBJECT_RE.match(subject or ""))


def _truncate_words(text: str, limit: int) -> str:
    """Cut at a word boundary within ``limit`` chars, with an explicit marker so
    the model knows the excerpt is partial (never silently truncated)."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut + _TRUNCATION_MARKER


@dataclass
class _Slice:
    """A single source's contribution, produced in its own worker thread."""

    items: list[tuple[str, DossierItem]] = field(default_factory=list)
    reviewed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Structured financial totals a source contributes (e.g. "clio_financials",
    # "qb_financials"), merged in build() and compared by _reconcile().
    metrics: dict[str, Any] = field(default_factory=dict)

    def safe(self, label: str, fn: Callable[[], Any]) -> Any:
        """Run one pull; record + swallow failures so the dossier stays partial."""
        try:
            result = fn()
            self.reviewed.append(label)
            return result
        except Exception as exc:  # noqa: BLE001 - one bad pull must not sink the dossier
            self.errors.append(f"{label}: {exc}")
            logger.warning("dossier pull failed", extra={"extra_fields": {"pull": label}})
            return None


def _estateably_gate_note(exc: HttpError, estate_id: str) -> str | None:
    """Return a clear note when ``exc`` is Estateably's per-case access gate.

    Estateably enforces a per-case billing/entitlement gate: gated cases return
    ``402 {"title": "Missing payment for case <id>"}`` on every estate
    sub-resource (and ``404 "Estate not found"`` on a couple). Detecting it on
    the first (summary) pull lets the caller emit ONE accurate note and skip the
    ~10 identically-gated follow-up calls, instead of the handful of confusing
    "402/404 API errors" the synthesizer would otherwise paraphrase. Returns
    ``None`` for any other error so the caller records it the normal way.
    """
    status = getattr(exc, "status", None)
    if status not in (402, 404):
        return None
    title = ""
    try:
        errors = json.loads(exc.body or "").get("errors") or []
        title = (errors[0].get("title") or "") if errors else ""
    except (ValueError, TypeError, AttributeError):
        title = ""
    if status == 402:
        return (
            f"estateably: case {estate_id} is under a billing/payment hold in Estateably "
            f"(HTTP 402: {title or 'Missing payment for case'!r}) — records not retrievable "
            f"via API. Verify the case's Estateably subscription/billing status."
        )
    return (
        f"estateably: estate {estate_id} not found via API (HTTP 404"
        f"{': ' + repr(title) if title else ''}) — verify the case exists and is accessible "
        f"to the integration account."
    )


def _best_ref_ids(candidate: MatterCandidate, term: str | None) -> dict[str, str]:
    """Most relevant record id per source system for this candidate.

    A candidate can carry more than one ref from a system (correlation grouped a
    couple of same-surname cases). A plain ``{system: id}`` comprehension silently
    keeps whichever ref is *last*, which is how a "David Yurkiw" search ended up
    reading a different "David …" estate. Pick, per system, the ref whose label
    best matches the query term so the dossier reads the intended matter.
    """
    want = _normalize(term or candidate.name)
    best: dict[str, str] = {}
    best_score: dict[str, int] = {}
    for r in candidate.refs:
        score = len(_normalize(r.label) & want)
        if r.system not in best or score > best_score[r.system]:
            best[r.system] = r.id
            best_score[r.system] = score
    return best


def _matter_title(candidate: MatterCandidate, ref_ids: dict[str, str]) -> str:
    """The status-file title — taken only from refs the dossier actually pulls.

    Correlation can group more than one client's refs under one candidate when
    they share a surname (a small "Bell" result set escapes the common-token
    guard in resolve._correlate). ``candidate.name`` is the longest label across
    ALL of a candidate's refs, chosen independently of ``_best_ref_ids`` — so on
    such a group the title could name a DIFFERENT matter than the one whose
    content was pulled (a "Desmond Bell" search titled "Michael Bell Trust -
    240018" over Desmond's estate). Restrict the title to the labels of the
    pulled refs, so the header always matches the content; among those (the same
    matter/client) keep the most descriptive = longest label.
    """
    pulled = set(ref_ids.items())
    labels = [r.label for r in candidate.refs if (r.system, r.id) in pulled]
    return max(labels, key=len) if labels else candidate.name


class DossierBuilder:
    """Assemble a :class:`MatterDossier` from the source connectors."""

    def __init__(
        self,
        *,
        estateably=None,
        clio=None,
        quickbooks=None,
        graph=None,
        knowledge_base=None,
        limits: DossierLimits = DEFAULT_LIMITS,
        # One worker per source so none queues behind the others (there are up to
        # 5: Estateably, Clio, QuickBooks, Outlook, SharePoint). They're I/O-bound.
        max_workers: int = 8,
    ) -> None:
        self._estateably = estateably
        self._clio = clio
        self._quickbooks = quickbooks
        self._graph = graph
        # Bedrock Knowledge Base client for SharePoint documents (full-body index).
        # None ⇒ SharePoint is skipped (e.g. before the index is provisioned).
        self._knowledge_base = knowledge_base
        self._limits = limits
        self._max_workers = max_workers

    def build(
        self, candidate: MatterCandidate, *, correspondence_term: str | None = None
    ) -> MatterDossier:
        ref_ids = _best_ref_ids(candidate, correspondence_term)
        dossier = MatterDossier(
            matter_name=_matter_title(candidate, ref_ids),
            matter_type=candidate.matter_type,
            refs=candidate.refs,
        )

        is_trust = estateably_is_trust(candidate.matter_type, candidate.name)
        jobs: list[tuple[str, Callable[[], _Slice]]] = []
        if self._estateably is not None and "estateably" in ref_ids:
            est_id = ref_ids["estateably"]
            jobs.append(("estateably", lambda: self._collect_estateably(est_id, is_trust)))
        # Clio client-contact id from the chosen Clio ref. QuickBooks keys its customer
        # per CLIENT (contact), stamping this id — NOT the matter id — into the customer
        # DisplayName as "(Clio <id>)". It drives BOTH the QuickBooks customer match and
        # the client-level Clio reconciliation total (a client's matters all bill under
        # the one QuickBooks customer). Stable across Clio renames.
        qb_customer_id = next(
            (
                r.qb_customer_id
                for r in candidate.refs
                if r.system == "clio" and r.id == ref_ids.get("clio")
            ),
            None,
        )
        if self._clio is not None and "clio" in ref_ids:
            jobs.append(("clio", lambda: self._collect_clio(ref_ids["clio"], qb_customer_id)))
        if self._quickbooks is not None:
            jobs.append(
                ("quickbooks", lambda: self._collect_quickbooks(candidate.name, qb_customer_id))
            )
        # Outlook correspondence: full-body search across all mailboxes.
        if self._graph is not None and correspondence_term:
            jobs.append(("graph", lambda: self._collect_graph(correspondence_term)))
        # SharePoint documents: semantic retrieval from the Bedrock Knowledge Base,
        # scoped to this matter (the index is firm-wide, so unscoped retrieval leaks
        # other clients' documents — a confidentiality issue).
        if self._knowledge_base is not None and correspondence_term:
            sp_numbers, sp_tokens = _matter_scope_keys(candidate, correspondence_term)
            if sp_numbers or sp_tokens:
                jobs.append(
                    (
                        "sharepoint",
                        lambda: self._collect_sharepoint(
                            correspondence_term, sp_numbers, sp_tokens
                        ),
                    )
                )
            else:
                # Fail closed: with no matter number or client-name tokens we cannot
                # tell whose documents a firm-wide retrieval returns — skip the pull
                # and say so, rather than risk another client's files in the dossier.
                dossier.sources_reviewed.append(
                    "SharePoint: skipped — no matter number or client-name scope "
                    "could be derived for this matter"
                )

        # One thread per source (connectors/rate-limiters are not thread-safe, so
        # a connector is only ever touched by a single thread). Each source times
        # its own wall-clock into ``timings`` (distinct keys ⇒ thread-safe).
        timings: dict[str, float] = {}

        def _timed(name: str, thunk: Callable[[], _Slice]) -> Callable[[], _Slice]:
            def run() -> _Slice:
                t0 = time.perf_counter()
                try:
                    return thunk()
                finally:
                    timings[name] = round((time.perf_counter() - t0) * 1000, 1)

            return run

        slices: list[_Slice] = []
        if jobs:
            with ThreadPoolExecutor(max_workers=min(self._max_workers, len(jobs))) as ex:
                slices = [f.result() for f in [ex.submit(_timed(n, j)) for n, j in jobs]]

        metrics: dict[str, Any] = {}
        for sl in slices:
            for section_key, item in sl.items:
                dossier.section(section_key).items.append(item)
            dossier.sources_reviewed.extend(sl.reviewed)
            dossier.errors.extend(sl.errors)
            metrics.update(sl.metrics)
        dossier.timings = timings
        _reconcile(dossier, metrics)
        _estateably_self_check(dossier, metrics)
        _compute_tax_deadlines(dossier, metrics)
        return dossier

    def _bounded(
        self,
        sl: _Slice,
        section: str,
        system: str,
        rows: Any,
        limit: int,
        *,
        id_key: str,
        summarize: Callable[[dict[str, Any]], str],
        date_keys: tuple[str, ...],
        attrs: bool = False,
        url_of: Callable[[dict[str, Any]], str | None] | None = None,
    ) -> None:
        """Append up to ``limit`` most-recent rows as items; mark truncation.

        ``url_of`` optionally computes a source deep-link per row (stashed in
        ``data['url']`` for citation linking) — a constant matter/estate URL for
        Clio/Estateably, or a per-row ``webUrl`` for SharePoint.
        """
        rows = rows or []
        if len(rows) > limit:
            sl.items.append((section, DossierItem(system, "-", f"(+{len(rows) - limit} more)")))
        for row in rows[:limit]:
            body = _attrs(row) if attrs else row
            rid = str(row.get(id_key) or body.get(id_key) or "-")
            url = url_of(body) if url_of else None
            sl.items.append(
                (
                    section,
                    DossierItem(
                        system,
                        rid,
                        summarize(body),
                        _first(body, *date_keys),
                        {"url": url} if url else {},
                    ),
                )
            )

    # -- Estateably ---------------------------------------------------------

    def _collect_estateably(self, estate_id: str, is_trust: bool = False) -> _Slice:
        sl = _Slice()
        c = self._estateably
        lim = self._limits
        # Every Estateably datum belongs to this estate → cite them all to the estate.
        estate_url = estateably_estate_url(
            getattr(c, "base_url", None), str(estate_id), is_trust=is_trust
        )

        # The summary is the canary for Estateably's per-case access gate. If the
        # case is gated (402 billing hold, or 404 estate-not-found) every estate
        # sub-resource returns the same error, so emit one clear note and skip the
        # rest of the fan-out rather than firing ~10 identically-failing calls.
        try:
            summ = c.get_estate_summary(estate_id)
            sl.reviewed.append("estateably:summary")
        except Exception as exc:  # noqa: BLE001 - one bad pull must not sink the dossier
            note = (
                _estateably_gate_note(exc, str(estate_id)) if isinstance(exc, HttpError) else None
            )
            if note:
                sl.errors.append(note)
                logger.warning(
                    "estateably case gated; skipping sub-resource pulls",
                    extra={
                        "extra_fields": {
                            "estate_id": str(estate_id),
                            "status": getattr(exc, "status", None),
                        }
                    },
                )
                return sl
            sl.errors.append(f"estateably:summary: {exc}")
            logger.warning(
                "dossier pull failed", extra={"extra_fields": {"pull": "estateably:summary"}}
            )
            summ = None
        if summ:
            a = _attrs(summ)
            sl.items.append(
                (
                    "estate_summary",
                    DossierItem(
                        "estateably",
                        str(estate_id),
                        _estate_summary_line(a),
                        None,
                        {**_redact_pii(a), "url": estate_url},
                    ),
                )
            )
            # Stated totals for the deterministic internal check (build() compares
            # them against the component sums accumulated below).
            sl.metrics["estateably_financials"] = {
                "stated_inventory": a.get("currentInventory"),
                "stated_estate_value": a.get("estateValue"),
            }

        wf = sl.safe("estateably:workflow", lambda: c.get_workflow(estate_id))
        if wf:
            nodes = _attrs(wf).get("nodes") or []
            sl.items.append(
                (
                    "estate_summary",
                    DossierItem(
                        "estateably",
                        str(estate_id),
                        f"workflow nodes: {len(nodes)}",
                        None,
                        {"url": estate_url},
                    ),
                )
            )

        intake = sl.safe("estateably:client_intake", lambda: c.get_client_intake(estate_id))
        if intake is not None:
            a = _attrs(intake)
            sl.items.append(
                (
                    "kyc",
                    DossierItem(
                        "estateably",
                        str(estate_id),
                        f"client intake: {_first(a, 'status') or 'present'}",
                        None,
                        {**_redact_pii(a), "url": estate_url},
                    ),
                )
            )
            # Date of death drives the code-computed terminal-T1 deadline (build()
            # calls _compute_tax_deadlines); it otherwise lives only inside the
            # redacted intake blob. Carry Estateably's own deadline too, to cross-check.
            sl.metrics["estate_dates"] = {
                "date_of_death": a.get("dateOfDeath"),
                "estateably_terminal_t1": a.get("terminalReturnFilingDeadline"),
            }

        self._bounded(
            sl,
            "kyc",
            "estateably",
            sl.safe("estateably:documents", lambda: c.list_documents(estate_id)),
            lim.documents,
            id_key="id",
            attrs=True,
            summarize=lambda a: _mask_free_text(
                f"doc: {_first(a, 'name', 'title', 'fileName') or 'unnamed'}"
            ),
            date_keys=("updatedAt", "createdAt", "date"),
            url_of=lambda _b: estate_url,
        )
        for role, fn in (
            ("contacts", c.list_contacts),
            ("beneficiaries", c.list_beneficiaries),
            ("executors", c.list_executors),
        ):
            self._bounded(
                sl,
                "parties",
                "estateably",
                sl.safe(f"estateably:{role}", lambda fn=fn: fn(estate_id)),
                lim.parties,
                id_key="id",
                attrs=True,
                summarize=lambda a, role=role: (
                    f"{role[:-1]}: {_first(a, 'name', 'fullName', 'role') or 'unknown'}"
                ),
                date_keys=("updatedAt", "createdAt"),
                url_of=lambda _b: estate_url,
            )
        self._bounded(
            sl,
            "tasks",
            "estateably",
            sl.safe("estateably:tasks", lambda: c.list_tasks(estate_id)),
            lim.tasks,
            id_key="id",
            attrs=True,
            summarize=lambda a: _mask_free_text(
                f"task: {_first(a, 'name', 'title') or '?'} [{_first(a, 'status') or ''}]"
            ),
            date_keys=("dueDate", "updatedAt", "createdAt"),
            url_of=lambda _b: estate_url,
        )
        # Accounts carry their balance in meta.balance.value (a sibling of
        # attributes), so read the FULL row (attrs=False) — the generic attrs=True
        # path would drop the balance and render "account: item:" with no figure.
        accounts = sl.safe("estateably:list_accounts", lambda: c.list_accounts(estate_id))
        self._bounded(
            sl,
            "financials",
            "estateably",
            accounts,
            lim.transactions,
            id_key="id",
            attrs=False,
            summarize=_estateably_account_summary,
            date_keys=(),
            url_of=lambda _b: estate_url,
        )
        # Inventory fields (value, category, inventoryType, capital/income) all live
        # under attributes → the normal attrs=True path.
        inventory = sl.safe("estateably:list_inventory", lambda: c.list_inventory(estate_id))
        self._bounded(
            sl,
            "financials",
            "estateably",
            inventory,
            lim.transactions,
            id_key="id",
            attrs=True,
            summarize=_estateably_inventory_summary,
            date_keys=("updatedAt", "createdAt", "date"),
            url_of=lambda _b: estate_url,
        )
        # Component sums for the deterministic internal check — computed over the
        # FULL pulls (the display above may be truncated to the most recent N).
        fin = sl.metrics.setdefault("estateably_financials", {})
        fin.update(_estateably_component_sums(accounts or [], inventory or []))
        self._bounded(
            sl,
            "activity",
            "estateably",
            sl.safe("estateably:comments", lambda: c.list_comments(estate_id)),
            lim.notes,
            id_key="id",
            attrs=True,
            summarize=lambda a: (
                f"note: {_mask_free_text(str(_first(a, 'body', 'text', 'comment') or ''))[:200]}"
            ),
            date_keys=("createdAt", "updatedAt"),
            url_of=lambda _b: estate_url,
        )
        return sl

    # -- Clio ---------------------------------------------------------------

    def _collect_clio(self, matter_id: str, client_id: str | None = None) -> _Slice:
        sl = _Slice()
        c = self._clio
        lim = self._limits
        # Every Clio datum belongs to this matter → cite them all to the matter in Clio.
        matter_url = clio_matter_url(getattr(c, "base_url", None), str(matter_id))

        matter = sl.safe(
            "clio:matter",
            lambda: c.get_matter(matter_id, fields="id,display_number,description,status"),
        )
        if matter:
            sl.items.append(
                (
                    "estate_summary",
                    DossierItem(
                        "clio",
                        str(matter_id),
                        (
                            f"{_first(matter, 'display_number') or ''} "
                            f"{_first(matter, 'description') or ''} "
                            f"[{_first(matter, 'status') or ''}]"
                        ).strip(),
                        None,
                        {**_redact_pii(matter), "url": matter_url},
                    ),
                )
            )

        self._bounded(
            sl,
            "tasks",
            "clio",
            sl.safe("clio:tasks", lambda: c.list_tasks(matter_id)),
            lim.tasks,
            id_key="id",
            summarize=lambda r: (
                f"task: {_mask_free_text(str(_first(r, 'name', 'description') or '?'))} "
                f"[{_first(r, 'status') or ''}]"
            ),
            date_keys=("due_at", "updated_at", "created_at"),
            url_of=lambda _b: matter_url,
        )
        self._bounded(
            sl,
            "deadlines",
            "clio",
            sl.safe("clio:calendar", lambda: c.list_calendar_entries(matter_id)),
            lim.calendar,
            id_key="id",
            summarize=lambda r: _mask_free_text(f"event: {_first(r, 'summary', 'title') or '?'}"),
            date_keys=("start_at", "created_at"),
            url_of=lambda _b: matter_url,
        )
        self._bounded(
            sl,
            "activity",
            "clio",
            sl.safe("clio:notes", lambda: c.list_matter_notes(matter_id)),
            lim.notes,
            id_key="id",
            summarize=lambda r: (
                f"note: {_mask_free_text(str(_first(r, 'detail', 'subject') or ''))[:200]}"
            ),
            date_keys=("created_at", "updated_at"),
            url_of=lambda _b: matter_url,
        )
        # MATTER-scoped trust/operating ledger balances (Matter.account_balances).
        # The previous firm-wide list_bank_accounts pull attached the firm's pooled
        # trust balance (eight figures) to every matter — client feedback round 2:
        # "the pooled balance should not appear in matter output at all; what we
        # need is the matter-level trust ledger balance from Clio."
        balances = sl.safe(
            "clio:matter_balances", lambda: c.get_matter_account_balances(matter_id)
        )
        if balances:
            for b in balances:
                if not isinstance(b, dict):
                    continue
                # Read balance directly (not via _first, which treats a 0 balance
                # as missing) so a real zero shows as "bal 0" rather than "? bal".
                bal = b.get("balance")
                sl.items.append(
                    (
                        "financials",
                        DossierItem(
                            "clio",
                            str(b.get("id", "-")),
                            (
                                f"matter {str(b.get('type') or 'account').lower()} account: "
                                f"{_mask_free_text(str(b.get('name') or '?'))} "
                                f"bal {bal if bal is not None else '?'}"
                            ),
                            None,
                            {"url": matter_url},
                        ),
                    )
                )
            # No explicit sl.reviewed.append here — sl.safe() above already
            # recorded "clio:matter_balances" on a successful pull; appending it
            # again duplicated the line in sources_reviewed.
        elif balances is not None:  # pull worked, matter simply has no ledger rows
            sl.reviewed.append("Clio: no matter-level trust ledger balances on this matter")
        bills = sl.safe(
            "clio:bills",
            lambda: c.list_bills(matter_id, fields="id,number,total,balance,paid,state,issued_at"),
        )
        self._bounded(
            sl,
            "financials",
            "clio",
            bills,
            lim.transactions,
            id_key="id",
            summarize=lambda r: (
                f"bill: {_first(r, 'number') or ''} total {_first(r, 'total') or ''} "
                f"paid {_first(r, 'paid') or ''} [{_first(r, 'state') or ''}]"
            ),
            date_keys=("issued_at", "created_at"),
            url_of=lambda _b: matter_url,
        )
        # Per-bill detail for the Clio↔QuickBooks reconciliation (matched by invoice
        # number in _reconcile). QuickBooks keys its customer per CLIENT, so gather the
        # client's bills across ALL their matters (Clio's bills.json rejects a client_id
        # filter, so enumerate the client's matters and union their bills). A
        # single-matter client reduces to this matter. The displayed bill items above
        # stay scoped to this matter; only the reconciliation is client-wide.
        recon_bills = [(matter_id, b) for b in (bills or [])]
        matter_count = 1
        if client_id:
            client_matters = (
                sl.safe(
                    "clio:client_matters",
                    lambda: c.list_matters(client_id=client_id, status=None, fields="id"),
                )
                or []
            )
            matter_ids = [
                str(m.get("id")) for m in client_matters if isinstance(m, dict) and m.get("id")
            ]
            if matter_ids:
                matter_count = len(matter_ids)
                for mid in matter_ids:
                    if mid == str(matter_id):
                        continue  # this matter's bills were already pulled above
                    more = (
                        sl.safe(
                            f"clio:bills:{mid}",
                            lambda mid=mid: c.list_bills(mid, fields="id,number,total,paid,state"),
                        )
                        or []
                    )
                    recon_bills += [(mid, b) for b in more]
        # Keep only ACTIVE bills, deduped by invoice number. Clio can hold a deleted
        # duplicate of a bill number (e.g. a superseded draft); summing those
        # double-counted the invoice total. Draft/void/deleted bills never issue to
        # QuickBooks, so they're excluded from the reconciliation.
        active: dict[str, dict[str, Any]] = {}
        for mid, b in recon_bills:
            if not isinstance(b, dict):
                continue
            if str(b.get("state") or "").lower() in _INACTIVE_BILL_STATES:
                continue
            number = str(_first(b, "number") or "").strip()
            if not number or number in active:
                continue
            active[number] = {
                "number": number,
                "matter": mid,
                "total": _amount(_first(b, "total")),
                "paid": _amount(_first(b, "paid")),
            }
        if active:
            sl.metrics["clio_financials"] = {
                "bills": list(active.values()),
                "matters": matter_count,
            }
        return sl

    # -- Outlook correspondence (Graph) ------------------------------------

    def _collect_graph(self, term: str) -> _Slice:
        """Search every staff mailbox for the matter's correspondence.

        App-only Graph can't run one org-wide email search, so this fans out a
        bounded **full-body** KQL search across all mailboxes: a bare-term ``AND``
        query matches subject, body and attachment text (Graph relevance-ranked),
        requiring every token so unrelated mail is excluded. Noise is bounded by
        the per-mailbox ``top`` and the global ``correspondence`` cap rather than
        by restricting to the subject line. Per-mailbox failures are counted, not
        fatal. Results are the ``correspondence`` section that feeds the Outlook
        output sections.
        """
        sl = _Slice()
        g = self._graph
        lim = self._limits

        tokens = [t for t in term.split() if t]
        if not tokens:
            return sl
        # Bare-term AND (not subject:) so bodies + attachments are searched too;
        # requiring every token keeps unrelated mail out. Graph returns these
        # relevance-ranked and the caps below bound the volume.
        kql = " AND ".join(tokens)

        # list_users also warms the Graph token, so the parallel searches below
        # reuse the cached token (no concurrent refresh).
        # Enumerated live every search, so a newly-added staff mailbox is included
        # automatically — no static allow-list to maintain.
        # The roster is org-wide (query-independent) → cache it briefly across
        # searches. list_users normally warms the Graph token; on a cache hit the
        # token is still valid (roster TTL << token life), so the fan-out is safe.
        def _roster() -> list:
            return _cached("graph:roster", lambda: g.list_users(top=999))

        users = sl.safe("outlook:mailboxes", _roster) or []
        searchable = [u for u in users if _searchable_mailbox(u)]
        skipped = len(users) - len(searchable)
        mailboxes = searchable[: lim.mailboxes]
        over_cap = len(searchable) - len(mailboxes)

        def search_one(u: dict[str, Any]) -> tuple[str, str, list | None, str | None]:
            mailbox = u.get("mail", "?")
            try:
                msgs = g.search_mailbox(u["id"], kql, top=lim.messages_per_mailbox)
                return mailbox, u["id"], msgs, None
            except Exception as exc:  # noqa: BLE001 - one bad mailbox must not sink the fan-out
                return mailbox, str(u.get("id") or ""), None, str(exc)

        # Searching mailboxes one-by-one is the fan-out's long pole; run them
        # concurrently (read-only GETs share the warmed token/session safely).
        collected: list[tuple[dict[str, Any], str, str]] = []
        no_mailbox = 0  # account has no REST-addressable mailbox (4xx) — expected
        mailbox_errors = 0  # transient/real failures (5xx, timeouts) — worth flagging
        if mailboxes:
            with ThreadPoolExecutor(max_workers=min(_OUTLOOK_WORKERS, len(mailboxes))) as ex:
                for mailbox, uid, msgs, err in ex.map(search_one, mailboxes):
                    if msgs is None:
                        if err and any(code in err for code in ("400", "403", "404")):
                            no_mailbox += 1
                        else:
                            mailbox_errors += 1
                        continue
                    for m in msgs:
                        collected.append((m, mailbox, uid))

        # -- drop development/test correspondence ----------------------------
        # Emails exchanged with the dev team (@loop8.io) are POC noise, not client
        # matter conversations. Filter BEFORE dedup/selection so they never set the
        # "reviewed to" time, consume the correspondence cap, or count as dups.
        dev_excluded = sum(1 for m, _mb, _u in collected if _is_excluded_correspondent(m))
        if dev_excluded:
            collected = [t for t in collected if not _is_excluded_correspondent(t[0])]

        # -- dedup across mailboxes ------------------------------------------
        # The same message lands once per recipient mailbox (a widely-CC'd thread
        # showed up 7×), crowding the correspondence cap and evicting unique
        # threads. internetMessageId is the RFC id, identical across every copy;
        # messages without one fall back to a (subject, received, sender) key.
        unique: dict[str, dict[str, Any]] = {}
        for m, mailbox, uid in collected:
            key = str(m.get("internetMessageId") or "") or "|".join(
                (
                    str(m.get("subject") or ""),
                    str(m.get("receivedDateTime") or ""),
                    str(((m.get("from") or {}).get("emailAddress") or {}).get("address") or ""),
                )
            )
            entry = unique.get(key)
            if entry is None:
                unique[key] = {"m": m, "mailbox": mailbox, "uid": uid, "dups": 0}
            else:
                entry["dups"] += 1
        duplicates = len(collected) - len(unique)

        # Deterministic newest-first ordering (id tiebreak → reproducible runs).
        entries = sorted(
            unique.values(),
            key=lambda e: (
                str(e["m"].get("receivedDateTime") or ""),
                str(e["m"].get("internetMessageId") or e["m"].get("id") or ""),
            ),
            reverse=True,
        )

        # -- selection: per-conversation cap ----------------------------------
        # Newest-first, but one thread may take at most _CONVERSATION_CAP slots;
        # if slots remain after the walk, capped-thread messages backfill them.
        per_conv: dict[str, int] = {}
        selected: list[dict[str, Any]] = []
        capped: list[dict[str, Any]] = []
        for e in entries:
            if len(selected) >= lim.correspondence:
                break
            conv = str(e["m"].get("conversationId") or e["m"].get("id") or "")
            if per_conv.get(conv, 0) >= _CONVERSATION_CAP:
                capped.append(e)
                continue
            per_conv[conv] = per_conv.get(conv, 0) + 1
            selected.append(e)
        for e in capped:
            if len(selected) >= lim.correspondence:
                break
            selected.append(e)
        selected.sort(
            key=lambda e: (
                str(e["m"].get("receivedDateTime") or ""),
                str(e["m"].get("internetMessageId") or e["m"].get("id") or ""),
            ),
            reverse=True,
        )

        # -- hydrate full bodies for the selected messages ---------------------
        # The search fan-out fetches only bodyPreview (~255 chars), which lost
        # email substance; the few selected winners get their full uniqueBody
        # (the message's NEW content, quoted chain excluded) as plain text.
        hydrate_errors = 0

        def hydrate(e: dict[str, Any]) -> None:
            nonlocal hydrate_errors
            m = e["m"]
            try:
                full = g.get_message(
                    e["uid"], str(m.get("id")), select="uniqueBody,body", body_as_text=True
                )
                unique = str(((full.get("uniqueBody") or {}).get("content")) or "").strip()
                body = str(((full.get("body") or {}).get("content")) or "").strip()
                # A reply's substance is its new text (uniqueBody), with the quoted
                # chain excluded to avoid re-including history. A FORWARD is the
                # opposite: the substance IS the forwarded/quoted content, so use the
                # full body there or it reaches the model as only the cover note.
                if _is_forwarded_subject(str(m.get("subject") or "")):
                    e["forwarded"] = True
                    content = body or unique
                else:
                    content = unique or body
                e["body"] = _body_text(content)
            except Exception:  # noqa: BLE001 - degrade to the preview, don't sink the slice
                hydrate_errors += 1
                e["body"] = ""

        if selected:
            with ThreadPoolExecutor(max_workers=min(_OUTLOOK_WORKERS, len(selected))) as ex:
                list(ex.map(hydrate, selected))

        # -- emit dossier items (newest-first, budgeted) ------------------------
        remaining = _CORRESPONDENCE_CHAR_BUDGET
        for e in selected:
            m = e["m"]
            subject = m.get("subject") or "(no subject)"
            summary = _mask_free_text(
                f"{subject} — from {_sender_label(m)} (mailbox: {e['mailbox']})"
            )
            body = e.get("body") or " ".join(str(m.get("bodyPreview") or "").split())
            # Forwards get a larger excerpt so the forwarded content survives the cut
            # (the sender's cover note alone is ~600 chars; the substance follows it).
            cap = _FORWARD_BODY_BUDGET if e.get("forwarded") else _BODY_CHAR_BUDGET
            allowance = cap if remaining >= cap else _BODY_CHAR_FLOOR
            excerpt = _truncate_words(_mask_free_text(body), allowance) if body else ""
            remaining = max(0, remaining - len(excerpt))
            data: dict[str, Any] = {}
            if m.get("webLink"):
                data["url"] = m["webLink"]
            if excerpt:
                data["body"] = excerpt
            if e["dups"]:
                data["also_in"] = e["dups"]  # count of other mailboxes holding a copy
            # Short, stable citation id (mirrors the SharePoint "sp-" scheme). The
            # model echoes this in the structured correspondence list, so a large
            # matter's 80 entries stay compact instead of repeating 150-char Graph
            # message ids — which overflowed the response token budget on big files
            # (Bell). The real Outlook deep-link rides in data["url"].
            cite_id = "ol-" + hashlib.sha1(str(m.get("id") or "").encode()).hexdigest()[:12]
            sl.items.append(
                (
                    "correspondence",
                    DossierItem(
                        "outlook",
                        cite_id,
                        summary[:400],
                        _to_pacific(m.get("receivedDateTime")),
                        data,
                    ),
                )
            )
        if len(entries) > len(selected):
            extra = len(entries) - len(selected)
            sl.items.append(("correspondence", DossierItem("outlook", "-", f"(+{extra} more)")))

        # Deterministic run-cutoff line FIRST — the framework requires the model to
        # open its correspondence section with it verbatim ("reviewed to [date]").
        if entries:
            newest = max(str(m.get("receivedDateTime") or "") for m, _mb, _u in collected)
            # Report what actually reached the summary, not the pre-cap total: when
            # the correspondence cap trims the pool, say "{kept} of {found} … (oldest
            # N omitted)" so the count never overstates coverage.
            omitted = len(entries) - len(selected)
            if omitted > 0:
                count_phrase = (
                    f"{len(selected)} of {len(entries)} unique messages included "
                    f"(oldest {omitted} omitted for length)"
                )
            else:
                count_phrase = f"{len(entries)} unique messages"
            cutoff = (
                f"Outlook correspondence reviewed to {_pacific_human(newest)} Pacific — "
                f"{count_phrase} across {len(mailboxes)} mailboxes searched"
            )
            if duplicates:
                cutoff += f" ({duplicates} duplicate copies removed)"
            sl.reviewed.append(cutoff)
        else:
            sl.reviewed.append(
                f"Outlook correspondence searched across {len(mailboxes)} mailboxes — "
                "no matching messages found"
            )
        note = f"Outlook: {len(mailboxes)} mailboxes searched (subject + full body ~ {term})"
        if skipped:
            note += f"; {skipped} disabled/guest/no-mail skipped"
        if no_mailbox:
            note += f"; {no_mailbox} without an accessible mailbox"
        if over_cap:
            note += f"; {over_cap} over the {lim.mailboxes} cap not searched"
        if hydrate_errors:
            note += f"; {hydrate_errors} message bodies unavailable (preview used)"
        if dev_excluded:
            note += f"; {dev_excluded} Loop8 dev/test email(s) excluded"
        sl.reviewed.append(note)
        if mailbox_errors:
            sl.errors.append(f"outlook: {mailbox_errors} mailbox searches failed")
        return sl

    # -- SharePoint (documents) --------------------------------------------

    def _collect_sharepoint(
        self,
        term: str,
        matter_numbers: frozenset[str] = frozenset(),
        tokens: frozenset[str] = frozenset(),
    ) -> _Slice:
        """Retrieve the matter's SharePoint documents from the Bedrock Knowledge Base.

        App-only live search is blocked on HT's legacy NetDocs-migrated libraries,
        so SharePoint documents are indexed (full body text) into a Knowledge Base
        by a scheduled crawler (``backend.lambdas.sharepoint_crawler``) and
        retrieved semantically here. Each hit contributes its matching text chunk
        plus a deep-link back to SharePoint, into the ``documents`` section.

        The index is firm-wide with no matter metadata, so an unscoped semantic
        search pulls in other clients' documents. Each hit is filtered to the
        searched matter (:func:`belongs_to_matter`) using the matter number +
        client-name tokens; off-matter hits are dropped before they reach the
        dossier. HR/Finance content is excluded at crawl time; a defensive path
        check drops any that slipped through. A retrieval failure is recorded and
        skipped, not fatal — one dead source never sinks the dossier.
        """
        sl = _Slice()
        kb = self._knowledge_base
        lim = self._limits
        try:
            hits = kb.retrieve(term, top_k=lim.sharepoint_docs)
        except Exception as exc:  # noqa: BLE001 - a KB failure must not sink the dossier
            sl.errors.append(f"sharepoint: {exc}")
            logger.warning("sharepoint KB retrieve failed")
            return sl

        excluded = 0
        off_matter = 0
        for r in hits:
            web_url = r.web_url
            # Crawl-time exclusion is primary; re-check the path here in case any
            # HR/Finance document was indexed before the filter applied.
            if web_url and is_excluded({"webUrl": web_url}):
                excluded += 1
                continue
            name = r.name or (web_url or r.uri or "document").rstrip("/").rsplit("/", 1)[-1]
            # Confidentiality: keep only documents that belong to this matter.
            if not belongs_to_matter(
                web_url, name, r.text, matter_numbers=matter_numbers, tokens=tokens
            ):
                off_matter += 1
                continue
            snippet = " ".join(r.text.split())[:280]
            # Use an opaque id for the citation record_id — filenames contain spaces
            # and parentheses (e.g. "MD Mgt account (17794.700).xlsx") that break the
            # single-token "(system:record_id)" marker and leak fragments into the
            # prose. The human filename stays in the summary/note. Masking runs
            # AFTER scoping (belongs_to_matter needs the raw name) and before the
            # title/snippet reach the dossier — account numbers ride in both.
            doc_id = "sp-" + hashlib.sha1((r.uri or web_url or name).encode()).hexdigest()[:12]
            sl.items.append(
                (
                    "documents",
                    DossierItem(
                        "sharepoint",
                        doc_id,
                        _mask_free_text(f"{name}: {snippet}")[:320],
                        r.metadata.get("last_modified"),
                        {"url": web_url} if web_url else {},
                    ),
                )
            )
        kept = len(hits) - excluded - off_matter
        note = f"SharePoint: {kept} document excerpt(s) retrieved (matter-scoped)"
        if off_matter:
            note += f"; {off_matter} off-matter excluded"
        if excluded:
            note += f"; {excluded} HR/Finance excluded"
        sl.reviewed.append(note)
        return sl

    # -- QuickBooks (Slice 2) ----------------------------------------------

    def _collect_quickbooks(self, matter_name: str, qb_customer_id: str | None = None) -> _Slice:
        """Financial reconciliation: match the matter's QuickBooks customer and pull
        its invoices/payments into the ``financials`` section.

        The customer is matched by the **Clio client-contact id** stamped into its
        DisplayName ("(Clio <id>)") — QBO keys a customer per client contact, not per
        matter — falling back to name matching only when the matter has no
        client-contact id. A miss is a ``reviewed`` note, not an error. Invoice/payment
        totals are stashed in ``metrics`` for the deterministic Clio↔QuickBooks
        reconciliation (see :func:`_reconcile`).
        """
        sl = _Slice()
        q = self._quickbooks
        lim = self._limits

        # A dead/expired QuickBooks token must not degrade silently (client ask):
        # reconciliation is a core item. Record it as a dossier error so the UI +
        # audit show the source was unavailable, and — for a token/auth death — log
        # the distinctive CONNECTOR_TOKEN_FAILURE marker the health probe watches.
        try:
            if qb_customer_id:
                customers = q.find_customer_by_clio_id(qb_customer_id) or []
            else:
                token = _longest_token(matter_name)
                customers = (q.find_customers(token) or []) if token else []
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "token refresh failed" in msg.lower():
                logger.error(
                    "CONNECTOR_TOKEN_FAILURE quickbooks",
                    extra={"extra_fields": {"connector": "quickbooks", "error": msg}},
                )
            else:
                logger.warning("quickbooks unavailable", extra={"extra_fields": {"error": msg}})
            sl.errors.append(f"QuickBooks unavailable — reconciliation data missing ({msg})")
            return sl
        customer = _match_customer(customers, qb_customer_id, matter_name)
        if customer is None:
            which = f"Clio contact {qb_customer_id}" if qb_customer_id else f"'{matter_name}'"
            sl.reviewed.append(f"QuickBooks: no customer matched matter ({which})")
            return sl

        cid = str(customer.get("Id") or "-")
        name = customer.get("DisplayName") or customer.get("CompanyName") or "?"
        bal = customer.get("Balance")
        summary = f"QuickBooks customer: {name}"
        if bal is not None:
            summary += f" (balance {bal})"
        # All matched QuickBooks records link to the customer in QBO (best-effort).
        cust_url = quickbooks_customer_url(cid, base_url=getattr(q, "base_url", None))
        sl.items.append(
            ("financials", DossierItem("quickbooks", cid, summary, None, {"url": cust_url}))
        )

        invoices = sl.safe("quickbooks:invoices", lambda: q.list_invoices(customer_id=cid)) or []
        self._bounded(
            sl,
            "financials",
            "quickbooks",
            invoices,
            lim.transactions,
            id_key="Id",
            summarize=lambda r: (
                f"invoice {_first(r, 'DocNumber') or '?'} {_first(r, 'TotalAmt') or ''}"
            ).strip(),
            date_keys=("TxnDate",),
            url_of=lambda _b: cust_url,
        )
        payments = sl.safe("quickbooks:payments", lambda: q.list_payments(customer_id=cid)) or []
        self._bounded(
            sl,
            "financials",
            "quickbooks",
            payments,
            lim.transactions,
            id_key="Id",
            summarize=lambda r: (
                f"payment {_first(r, 'TotalAmt') or ''} on {_first(r, 'TxnDate') or ''}"
            ).strip(),
            date_keys=("TxnDate",),
            url_of=lambda _b: cust_url,
        )
        # Per-invoice detail for the reconciliation, keyed by the Clio bill number in
        # the DocNumber ("clio-<n>"; "QB-<n>" ⇒ QuickBooks-only). paid = TotalAmt −
        # Balance (per-invoice open amount) — QuickBooks Payment records carry no
        # invoice number, so Balance is the reliable per-invoice paid signal.
        sl.metrics["qb_financials"] = {
            "invoices": [
                {
                    "docnumber": _first(inv, "DocNumber"),
                    "clio_number": _clio_bill_number(_first(inv, "DocNumber")),
                    "invoiced": _amount(_first(inv, "TotalAmt")),
                    "paid": _amount(_first(inv, "TotalAmt")) - _amount(inv.get("Balance")),
                }
                for inv in invoices
                if isinstance(inv, dict)
            ]
        }
        sl.reviewed.append(f"QuickBooks: reconciled against customer {name}")
        return sl
