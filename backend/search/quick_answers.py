"""Tier-0 quick answers — deterministic single-matter lookups, no LLM.

Given a resolved matter and a narrow intent (from :mod:`backend.search.intent`),
answer with 1-2 direct connector calls and return the value with source
citations. No client data goes to any model, so nothing leaves Canada.

Each handler returns a :class:`QuickAnswer` (text + citations) or ``None`` to
decline — in which case the pipeline falls through to the full LLM synthesis
(Tier 2). Handlers never raise: a failing source (e.g. an Estateably 402) is
caught and either noted or causes a decline. Text carries inline
``(system:record_id)`` markers matching the citations, so the frontend renders
the same numbered-footnote UX as the status file.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from backend.search.deeplinks import (
    clio_matter_url,
    estateably_estate_url,
    estateably_is_trust,
)
from backend.search.orchestrator import (
    _attrs,
    _first,
    _is_excluded_correspondent,
    _searchable_mailbox,
    _sender_label,
    _to_pacific,
)
from backend.search.resolve import MatterCandidate
from services.logging import get_logger

logger = get_logger(__name__)

_MAX_MAILBOXES = 40
_MAX_ITEMS = 15  # cap list-style answers (documents, deadlines, emails)


@dataclass
class QuickAnswer:
    intent: str
    text: str
    citations: list[dict[str, Any]] = field(default_factory=list)


def _money(value: Any) -> str | None:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return None


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


class _Builder:
    """Accumulates answer text + de-duplicated citations, emitting inline markers."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.cites: list[dict[str, Any]] = []
        self._seen: set[tuple[str, str]] = set()
        self.notes: list[str] = []

    def cite(self, system: str, record_id: str, note: str, url: str | None = None) -> str:
        key = (system, str(record_id))
        if key not in self._seen:
            self._seen.add(key)
            entry: dict[str, Any] = {"system": system, "record_id": str(record_id), "note": note}
            if url:
                entry["url"] = url
            self.cites.append(entry)
        return f"({system}:{record_id})"

    def line(self, text: str) -> None:
        self.lines.append(text)

    def render(self) -> str:
        body = "\n".join(self.lines)
        if self.notes:
            body += "\n\n" + " ".join(f"_{n}_" for n in self.notes)
        return body


class QuickAnswerService:
    """Deterministic answers for narrow intents. Connectors are injected/optional."""

    def __init__(
        self,
        *,
        estateably: Any | None = None,
        clio: Any | None = None,
        graph: Any | None = None,
        quickbooks: Any | None = None,
    ) -> None:
        self._estateably = estateably
        self._clio = clio
        self._graph = graph
        self._quickbooks = quickbooks

    def answer(
        self,
        intent: str,
        candidate: MatterCandidate,
        *,
        term: str | None = None,
        query: str | None = None,
    ) -> QuickAnswer | None:
        handler = _HANDLERS.get(intent)
        if handler is None:
            return None
        try:
            return handler(self, candidate, term or candidate.name, query or "")
        except Exception:  # noqa: BLE001 - a quick answer must never break the request
            logger.warning(
                "quick answer handler failed", extra={"extra_fields": {"intent": intent}}
            )
            return None

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _refs(candidate: MatterCandidate) -> dict[str, str]:
        return {r.system: r.id for r in candidate.refs}

    def _estate_url(self, candidate: MatterCandidate, estate_id: str) -> str:
        is_trust = estateably_is_trust(candidate.matter_type, candidate.name)
        return estateably_estate_url(
            getattr(self._estateably, "base_url", None), str(estate_id), is_trust=is_trust
        )

    def _matter_url(self, matter_id: str) -> str:
        return clio_matter_url(getattr(self._clio, "base_url", None), str(matter_id))

    @staticmethod
    def _safe(b: _Builder, label: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            b.notes.append(f"Could not reach {label} ({type(exc).__name__}).")
            logger.info("quick answer source failed", extra={"extra_fields": {"source": label}})
            return None

    # -- handlers ----------------------------------------------------------

    def _balance(self, candidate: MatterCandidate, term: str, query: str) -> QuickAnswer | None:
        refs = self._refs(candidate)
        b = _Builder()
        got = False
        estate_id = refs.get("estateably")
        if self._estateably and estate_id:
            summ = self._safe(
                b, "Estateably", lambda: self._estateably.get_estate_summary(estate_id)
            )
            if summ:
                a = _attrs(summ)
                bal = _money(_first(a, "balance"))
                val = _money(_first(a, "estateValue", "taxableEstateValue"))
                if bal:
                    marker = b.cite(
                        "estateably",
                        estate_id,
                        "estate summary — balance",
                        self._estate_url(candidate, estate_id),
                    )
                    b.line(f"Cash/estate balance (Estateably): **{bal}**. {marker}")
                    got = True
                if val:
                    b.line(f"Estate value: {val}.")
        matter_id = refs.get("clio")
        if self._clio and matter_id:
            # MATTER-scoped balances (Matter.account_balances) — the firm-wide
            # bank_accounts pull leaked the pooled trust balance into matter output.
            accts = self._safe(
                b,
                "Clio",
                lambda: self._clio.get_matter_account_balances(matter_id),
            )
            for acct in accts or []:
                bal = _money(acct.get("balance"))
                if bal:
                    kind = str(acct.get("type") or "account").lower()
                    marker = b.cite(
                        "clio",
                        str(acct.get("id")),
                        f"Clio matter {kind} balance — {acct.get('name', 'account')}",
                        self._matter_url(matter_id),
                    )
                    b.line(f"- {acct.get('name', 'Account')} (matter {kind}): {bal} {marker}")
                    got = True
        if not got:
            return None
        return QuickAnswer("balance", b.render(), b.cites)

    def _parties(self, candidate: MatterCandidate, term: str, query: str) -> QuickAnswer | None:
        refs = self._refs(candidate)
        estate_id = refs.get("estateably")
        if not (self._estateably and estate_id):
            return None
        b = _Builder()
        url = self._estate_url(candidate, estate_id)
        bens = (
            self._safe(b, "Estateably", lambda: self._estateably.list_beneficiaries(estate_id))
            or []
        )
        exs = self._safe(b, "Estateably", lambda: self._estateably.list_executors(estate_id)) or []
        if not (bens or exs):
            return None
        if exs:
            b.line("**Executors / trustees:**")
            for e in exs[:_MAX_ITEMS]:
                name = _first(_attrs(e), "name", "fullName") or "unknown"
                m = b.cite("estateably", str(e.get("id", estate_id)), f"executor — {name}", url)
                b.line(f"- {name} {m}")
        if bens:
            b.line("\n**Beneficiaries:**")
            for be in bens[:_MAX_ITEMS]:
                a = _attrs(be)
                name = _first(a, "name", "fullName") or "unknown"
                rel = _first(a, "familyMemberType", "typeOfBeneficiary", "relationship")
                extra = f" ({rel})" if rel else ""
                m = b.cite("estateably", str(be.get("id", estate_id)), f"beneficiary — {name}", url)
                b.line(f"- {name}{extra} {m}")
        return QuickAnswer("parties", b.render(), b.cites)

    def _kyc(self, candidate: MatterCandidate, term: str, query: str) -> QuickAnswer | None:
        refs = self._refs(candidate)
        estate_id = refs.get("estateably")
        if not (self._estateably and estate_id):
            return None
        b = _Builder()
        url = self._estate_url(candidate, estate_id)
        intake = self._safe(b, "Estateably", lambda: self._estateably.get_client_intake(estate_id))
        bens = (
            self._safe(b, "Estateably", lambda: self._estateably.list_beneficiaries(estate_id))
            or []
        )
        exs = self._safe(b, "Estateably", lambda: self._estateably.list_executors(estate_id)) or []
        docs = self._safe(b, "Estateably", lambda: self._estateably.list_documents(estate_id)) or []
        if intake is None and not (bens or exs or docs):
            return None
        marker = b.cite("estateably", estate_id, "estate KYC / intake", url)
        b.line(f"KYC & intake for **{candidate.name}** {marker}")
        b.line(f"- Client intake record: {'present' if intake else 'not found'}.")
        b.line(f"- Parties requiring KYC: {len(exs)} executor(s), {len(bens)} beneficiary(ies).")
        doc_names = [(_attrs(d).get("name") or _attrs(d).get("title") or "") for d in docs]
        kyc_docs = [
            n
            for n in doc_names
            if any(
                k in n.lower()
                for k in ("kyc", "intake", "id", "identification", "passport", "licence", "license")
            )
        ]
        b.line(
            f"- Documents on file: {len(docs)}; KYC/ID-type documents detected: {len(kyc_docs)}."
        )
        if kyc_docs:
            for n in kyc_docs[:_MAX_ITEMS]:
                b.line(f"  - {n}")
        b.notes.append("Deterministic check — verify completeness against the KYC folder.")
        return QuickAnswer("kyc", b.render(), b.cites)

    def _deadlines(self, candidate: MatterCandidate, term: str, query: str) -> QuickAnswer | None:
        refs = self._refs(candidate)
        b = _Builder()
        today = _today()
        rows: list[tuple[str, str, str]] = []  # (date, text, marker)
        estate_id = refs.get("estateably")
        if self._estateably and estate_id:
            url = self._estate_url(candidate, estate_id)
            tasks = (
                self._safe(b, "Estateably", lambda: self._estateably.list_tasks(estate_id)) or []
            )
            for t in tasks:
                a = _attrs(t)
                due = _first(a, "dueDate", "due")
                if due and str(due)[:10] >= today:
                    name = _first(a, "name", "title") or "task"
                    rows.append(
                        (
                            str(due)[:10],
                            name,
                            b.cite(
                                "estateably", str(t.get("id", estate_id)), f"task — {name}", url
                            ),
                        )
                    )
        matter_id = refs.get("clio")
        if self._clio and matter_id:
            url = self._matter_url(matter_id)
            cal = (
                self._safe(
                    b,
                    "Clio",
                    lambda: self._clio.list_calendar_entries(
                        matter_id, fields="id,summary,start_at"
                    ),
                )
                or []
            )
            for e in cal:
                start = e.get("start_at") or ""
                if start and start[:10] >= today:
                    rows.append(
                        (
                            start[:10],
                            e.get("summary") or "event",
                            b.cite("clio", str(e.get("id")), "Clio calendar entry", url),
                        )
                    )
        if not rows:
            return None
        rows.sort(key=lambda r: r[0])
        b.line(f"**Upcoming deadlines for {candidate.name}:**")
        for date, text, marker in rows[:_MAX_ITEMS]:
            b.line(f"- {date} — {text} {marker}")
        return QuickAnswer("deadlines", b.render(), b.cites)

    def _documents(self, candidate: MatterCandidate, term: str, query: str) -> QuickAnswer | None:
        refs = self._refs(candidate)
        estate_id = refs.get("estateably")
        if not (self._estateably and estate_id):
            return None
        b = _Builder()
        url = self._estate_url(candidate, estate_id)
        docs = self._safe(b, "Estateably", lambda: self._estateably.list_documents(estate_id))
        if not docs:
            return None
        m = b.cite("estateably", estate_id, "documents", url)
        b.line(f"**Documents on file: {len(docs)}** {m}")
        for d in docs[:_MAX_ITEMS]:
            a = _attrs(d)
            name = _first(a, "name", "title", "fileName") or "unnamed"
            date = _first(a, "updatedAt", "createdAt", "date")
            b.line(f"- {name}{f' ({str(date)[:10]})' if date else ''}")
        if len(docs) > _MAX_ITEMS:
            b.line(f"- …and {len(docs) - _MAX_ITEMS} more.")
        return QuickAnswer("documents", b.render(), b.cites)

    def _tax_status(self, candidate: MatterCandidate, term: str, query: str) -> QuickAnswer | None:
        refs = self._refs(candidate)
        estate_id = refs.get("estateably")
        if not (self._estateably and estate_id):
            return None
        b = _Builder()
        url = self._estate_url(candidate, estate_id)
        docs = self._safe(b, "Estateably", lambda: self._estateably.list_documents(estate_id)) or []
        kw = ("t1", "t3", "noa", "notice of assessment", "cra", "clearance", "tax")
        hits = []
        for d in docs:
            name = _attrs(d).get("name") or _attrs(d).get("title") or ""
            if any(k in name.lower() for k in kw):
                hits.append((name, _first(_attrs(d), "updatedAt", "createdAt", "date")))
        if not hits:
            return None
        m = b.cite("estateably", estate_id, "tax documents", url)
        b.line(f"**Tax-related records on file: {len(hits)}** {m}")
        for name, date in hits[:_MAX_ITEMS]:
            b.line(f"- {name}{f' ({str(date)[:10]})' if date else ''}")
        b.notes.append("Filed/assessed status is not inferred — confirm against the Tax folder.")
        return QuickAnswer("tax_status", b.render(), b.cites)

    def _bills(self, candidate: MatterCandidate, term: str, query: str) -> QuickAnswer | None:
        refs = self._refs(candidate)
        matter_id = refs.get("clio")
        if not (self._clio and matter_id):
            return None
        b = _Builder()
        url = self._matter_url(matter_id)
        bills = self._safe(
            b,
            "Clio",
            lambda: self._clio.list_bills(matter_id, fields="id,number,state,total,balance"),
        )
        if not bills:
            return None
        outstanding = [
            bl for bl in bills if _money(bl.get("balance")) and float(bl.get("balance") or 0) > 0
        ]
        m = b.cite("clio", matter_id, "Clio bills", url)
        b.line(f"**Bills — {len(bills)} total, {len(outstanding)} outstanding.** {m}")
        for bl in outstanding[:_MAX_ITEMS]:
            num = bl.get("number", bl.get("id"))
            b.line(f"- Bill {num}: {_money(bl.get('balance'))} outstanding ({bl.get('state', '')})")
        return QuickAnswer("bills", b.render(), b.cites)

    def _correspondence(
        self, candidate: MatterCandidate, term: str, query: str
    ) -> QuickAnswer | None:
        if not self._graph:
            return None
        tokens = [t for t in (term or candidate.name).split() if t]
        if not tokens:
            return None
        b = _Builder()
        # Bare-term AND (not subject:) so message bodies are searched too.
        kql = " AND ".join(tokens)
        users = self._safe(b, "Outlook", lambda: self._graph.list_users(top=999)) or []
        mailboxes = [u for u in users if _searchable_mailbox(u)][:_MAX_MAILBOXES]
        if not mailboxes:
            return None

        def search_one(u: dict[str, Any]) -> list[dict[str, Any]]:
            try:
                return self._graph.search_mailbox(u["id"], kql, top=5) or []
            except Exception:  # noqa: BLE001 - one bad mailbox must not sink the fan-out
                return []

        collected: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(8, len(mailboxes))) as ex:
            for msgs in ex.map(search_one, mailboxes):
                collected.extend(msgs)
        if not collected:
            return None
        collected.sort(key=lambda m: m.get("receivedDateTime") or "", reverse=True)
        # Dedup the cross-mailbox copies of the same message (same RFC id), and
        # drop dev/test correspondence (emails exchanged with @loop8.io).
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for m in collected:
            if _is_excluded_correspondent(m):
                continue
            key = str(m.get("internetMessageId") or m.get("id") or "")
            if key in seen:
                continue
            seen.add(key)
            deduped.append(m)
        if not deduped:
            return None
        b.line(f"**Recent correspondence matching '{term}':**")
        for m in deduped[:_MAX_ITEMS]:
            frm = _sender_label(m)
            subj = m.get("subject") or "(no subject)"
            date = str(_to_pacific(m.get("receivedDateTime")) or "")[:10]
            marker = b.cite("outlook", str(m.get("id", "-")), f"{subj} — {frm}", m.get("webLink"))
            b.line(f"- {date} — {subj} — {frm} {marker}")
        return QuickAnswer("correspondence", b.render(), b.cites)

    def _profile(self, candidate: MatterCandidate, term: str, query: str) -> QuickAnswer | None:
        refs = self._refs(candidate)
        estate_id = refs.get("estateably")
        if not (self._estateably and estate_id):
            return None
        b = _Builder()
        intake = self._safe(b, "Estateably", lambda: self._estateably.get_client_intake(estate_id))
        if not intake:
            return None
        a = _attrs(intake)
        name_parts = [_first(a, "firstName"), _first(a, "firstMiddleName"), _first(a, "lastName")]
        full_name = " ".join(str(p) for p in name_parts if p) or candidate.name
        # Note: SIN (sinNumber) is deliberately never surfaced.
        facts = {
            "Date of birth": _first(a, "birthDate"),
            "Date of death": _first(a, "dateOfDeath"),
            "Marital status": _first(a, "maritalStatus"),
            "Province": _first(a, "province"),
            "Terminal T1 filing deadline": _first(a, "terminalReturnFilingDeadline"),
            "Estate (T3) return filing deadline": _first(a, "estateReturnFilingDeadline"),
        }

        def _fmt(v: Any) -> str:
            s = str(v)
            return s[:10] if "T" in s and len(s) >= 10 else s  # trim ISO datetimes to a date

        q = query.lower()
        lead_label = None
        if "birth" in q or "born" in q or "dob" in q:
            lead_label = "Date of birth"
        elif "death" in q or "die" in q or "passed" in q or "dod" in q:
            lead_label = "Date of death"
        elif "marital" in q:
            lead_label = "Marital status"
        elif "province" in q or "jurisdiction" in q or "domicile" in q:
            lead_label = "Province"

        m = b.cite("estateably", estate_id, "client intake", self._estate_url(candidate, estate_id))
        if lead_label:
            val = facts.get(lead_label)
            shown = _fmt(val) if val else "not recorded in the Estateably intake"
            b.line(f"**{lead_label} of {full_name}: {shown}** {m}")
        else:
            b.line(f"**Matter details for {full_name}** {m}")
        b.line("")
        for label, val in facts.items():
            if label == lead_label or not val:
                continue
            b.line(f"- {label}: {_fmt(val)}")
        return QuickAnswer("profile", b.render(), b.cites)


_HANDLERS: dict[
    str, Callable[[QuickAnswerService, MatterCandidate, str, str], QuickAnswer | None]
] = {
    "balance": QuickAnswerService._balance,
    "parties": QuickAnswerService._parties,
    "kyc": QuickAnswerService._kyc,
    "deadlines": QuickAnswerService._deadlines,
    "documents": QuickAnswerService._documents,
    "tax_status": QuickAnswerService._tax_status,
    "bills": QuickAnswerService._bills,
    "correspondence": QuickAnswerService._correspondence,
    "profile": QuickAnswerService._profile,
}
