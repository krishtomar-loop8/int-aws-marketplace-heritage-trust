"""Data-quality layer for the AML name export.

Everything that turns a raw party roster into a screening-ready, auditable
output lives here: which matters to exclude, how to normalize names/dates, and
the validation that produces the exception report. Kept separate from
``export.py`` (extraction) so the rules are unit-testable in isolation and easy
for Aphrodite to review.

No PII is ever logged from this module; it only shapes the workbook data.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from datetime import date, datetime
from typing import Any

# -- exclusions --------------------------------------------------------------

INACTIVE_STATUSES = {"closed", "archived", "inactive", "terminated", "superseded"}

# Internal / administrative matters that must never be screened as client parties.
INTERNAL_TITLE_KEYWORDS = (
    "accounting",
    "bcfsa communication",
    "finance & compliance",
    "finance and compliance",
    "board of directors",
    "wills bank",
    "suspense",
    "natalie's files",
    "natalies files",
    "aphrodite - administration",
    "aphrodite administration",
)
_INTERNAL_NUMBER_RE = re.compile(r"^\s*(2100-\d+|999999)\b")


def is_inactive_status(status: str | None) -> bool:
    return str(status or "").strip().lower() in INACTIVE_STATUSES


def exclusion_reason(
    *,
    matter_number: str | None,
    description: str | None,
    title: str | None,
    status: str | None,
) -> str | None:
    """Return why a matter is excluded from screening, or ``None`` to keep it."""
    if is_inactive_status(status):
        return f"Inactive matter (status: {str(status).strip().lower()})"
    haystack = f"{title or ''} {description or ''}".lower()
    if "do not use" in haystack:
        return "Marked DO NOT USE"
    if re.search(r"\btest\b", haystack):
        return "Test matter"
    num = str(matter_number or "")
    if _INTERNAL_NUMBER_RE.match(num) or any(k in haystack for k in INTERNAL_TITLE_KEYWORDS):
        return "Internal / administrative matter"
    return None


# -- role taxonomy + named / entity classification --------------------------

# Aphrodite's authoritative screening taxonomy (Nicole, 2026-07-15/20). A party
# whose source role does not map to one of these is NOT screened — it is routed
# to the Excluded Parties view with a reason, and its raw role text is retained
# in a separate ``Source Role`` column.
SCREENING_ROLES = (
    "Deceased",
    "Settlor",
    "Trustee",
    "Executor",
    "Beneficiary",
    "Protector",
    "Attorney",
    "Committee",
    "UBO",
    "Counterparty",
)
_CANONICAL_LOWER = {r.lower(): r for r in SCREENING_ROLES}

# Estateably emits camelCase source labels ("accountantCpa", "realEstateAgent").
# Whole-word rules only fire once those are split into separate tokens, so both
# map_role and is_non_principal_role tokenize role text through _role_words —
# keeping the mapping aligned across source labels and party types (Nicole item 1).
_CAMEL_RE = re.compile(r"(?<=[a-z])(?=[A-Z])")


def _role_words(role: Any) -> str:
    """Lowercased role text with camelCase boundaries split to spaces, so
    whole-word matching catches Estateably's camelCase source labels
    (accountantCpa → 'accountant cpa', realEstateAgent → 'real estate agent')."""
    return _CAMEL_RE.sub(" ", normalize_text(role)).lower()


# Source role / category synonyms → canonical screening role. Matched first as an
# exact string, then as a whole-word token inside messy free text (e.g.
# "poa for barry haer jan26 2023" → Attorney, "executor named in will" → Executor).
_ROLE_SYNONYMS = {
    "deceased": "Deceased",
    "settlor": "Settlor",
    "grantor": "Settlor",
    "trustee": "Trustee",
    "co-trustee": "Trustee",
    "cotrustee": "Trustee",
    "executor": "Executor",
    "executrix": "Executor",
    "administrator": "Executor",
    "personal representative": "Executor",
    "estate trustee": "Executor",
    "beneficiary": "Beneficiary",
    "contingent beneficiary": "Beneficiary",
    "residual beneficiary": "Beneficiary",
    "protector": "Protector",
    "appointer": "Protector",
    "attorney": "Attorney",
    "poa": "Attorney",
    "power of attorney": "Attorney",
    "committee": "Committee",
    "committee of person": "Committee",
    "committee of estate": "Committee",
    "committeeship": "Committee",
    "ubo": "UBO",
    "beneficial owner": "UBO",
    "ultimate beneficial owner": "UBO",
    "counterparty": "Counterparty",
}


def map_role(source_role: Any) -> str | None:
    """Map a raw source role to the canonical screening taxonomy.

    Returns the canonical role, or ``None`` when the role is not a screenable
    principal (advisors, service providers, social contacts, and generic Clio
    "Client" / POA "Principal" all return ``None`` → Excluded Parties view).
    """
    text = _role_words(source_role)
    if not text:
        return None
    if text in _CANONICAL_LOWER:
        return _CANONICAL_LOWER[text]
    if text in _ROLE_SYNONYMS:
        return _ROLE_SYNONYMS[text]
    for needle, canonical in _ROLE_SYNONYMS.items():
        if re.search(rf"\b{re.escape(needle)}\b", text):
            return canonical
    return None


def is_excluded_party_name(name: Any) -> str | None:
    """Return a reason if this party is Aphrodite's own company (never screened).

    Matches only names that *start* with "Aphrodite" (the company acting as
    executor/trustee/client), so a client trust merely containing the word does
    not get suppressed."""
    if normalize_text(name).lower().startswith("aphrodite"):
        return "Aphrodite — own company, not a screened party"
    return None


# Clearly non-principal roles: advisors, service providers, and social contacts.
# These are excluded from screening (Nicole item 2 / decision D). Anything that is
# NEITHER a taxonomy role NOR clearly non-principal (generic "Client", POA
# "Principal", "brother - representative", blank, …) is instead KEPT in the
# screening set as "Role to be Confirmed" — a principal must never be dropped
# because the source role is incomplete (Nicole item 3).
_NON_PRINCIPAL_KEYWORDS = (
    "accountant",
    "cpa",
    "bookkeeper",
    "lawyer",
    "solicitor",
    "notary",
    "paralegal",
    "legal assistant",
    "real estate agent",
    "realtor",
    "appraiser",
    "banker",
    "bank rep",
    "bank representative",
    "financial advisor",
    "financial planner",
    "wealth advisor",
    "wealth manager",
    "investment advisor",
    "investment counsel",
    "portfolio manager",
    "insurance agent",
    "insurance broker",
    "broker",
    "planned giving",
    "fundraiser",
    "friend",
    "girlfriend",
    "boyfriend",
    "neighbour",
    "neighbor",
    "prospective buyer",
    "prospective renter",
    "prospective purchaser",
    "advisor",
    "adviser",
    "consultant",
    # Service providers / social-support roles surfaced in Aphrodite's review
    # (2026-07-27). camelCase source labels (realEstateAgent, bankRepresentative,
    # wealthAdvisor …) are handled by _role_words; these cover the rest.
    "wealth management",
    "social worker",
    "case worker",
    "case manager",
    "case coordinator",
    "care worker",
    "support worker",
    "occupational therapist",
    "therapist",
    "tenant",
    "group home manager",
    "counsel",
    # Wealth-planning advisor titles surfaced in Aphrodite's 2026-08-06 review
    # ("Nicola Wealth-Senior Wealth Planning Associate" — a firm-plus-title label
    # that the -advisor/-manager terms above didn't catch).
    "wealth planning",
    "wealth planner",
    "planning associate",
    "financial planning",
)


def is_non_principal_role(role: Any) -> bool:
    """True for a role that is clearly a non-principal (advisor / service provider
    / social contact) and should be excluded from screening. Reached only after
    :func:`map_role` returns ``None`` — ambiguous roles return ``False`` (kept)."""
    text = _role_words(role)
    if not text:
        return False
    return any(re.search(rf"\b{re.escape(k)}\b", text) for k in _NON_PRINCIPAL_KEYWORDS)


_CORP_TOKENS_RE = re.compile(
    r"\b(ltd|inc|incorporated|corporation|corp|company|co|llc|llp|lp|limited)\b", re.IGNORECASE
)
_ENTITY_TOKENS_RE = re.compile(
    r"\b(trust|foundation|society|association|holdings|"
    r"ltd|inc|incorporated|corporation|corp|company|llc|llp|limited)\b|estate of",
    re.IGNORECASE,
)


def looks_like_entity(name: Any) -> bool:
    """True when a party name is an organization/trust/estate, not a natural
    person — so we never stamp it as an Individual or attach a person DOB."""
    return bool(_ENTITY_TOKENS_RE.search(normalize_text(name)))


def entity_party_type(name: Any) -> str:
    """Best-effort party type for an entity name (Corporation vs Trust)."""
    norm = normalize_text(name)
    if _CORP_TOKENS_RE.search(norm):
        return "Corporation"
    if re.search(r"\btrust\b", norm, re.IGNORECASE):
        return "Trust"
    return "Corporation"


# Substantive (non-capitalization) misspellings worth flagging for source
# correction — distinct from the case-only check. Seeded from Aphrodite's review
# ("Country Historical Society" should be "County"); extend as new ones surface.
# Each entry: (wrong-word, likely-correct, context-that-must-also-appear).
_MISSPELLINGS = [
    (
        "country",
        "county",
        re.compile(r"\b(historical|society|regional|municipal|district|council)\b", re.IGNORECASE),
    ),
]


def _spelling_flags(name: str) -> list[str]:
    """Flag likely substantive misspellings (not merely unusual capitalization)."""
    issues: list[str] = []
    low = name.lower()
    for wrong, right, context in _MISSPELLINGS:
        if re.search(rf"\b{wrong}\b", low) and context.search(name):
            issues.append(
                f"Possible spelling error — '{wrong}' may be '{right}' (verify at source)"
            )
    return issues


# -- normalization -----------------------------------------------------------

_PUNCT_MAP = {
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "–": "-",
    "—": "-",
    "−": "-",
}


def normalize_text(value: Any) -> str:
    """Trim, collapse repeated/again-broken whitespace, drop non-breaking spaces,
    and normalize smart quotes/dashes. Safe for names and descriptions."""
    if value is None:
        return ""
    text = str(value)
    text = unicodedata.normalize("NFKC", text)  # folds NBSP → space, etc.
    for src, dst in _PUNCT_MAP.items():
        text = text.replace(src, dst)
    return " ".join(text.split())


def _had_formatting_noise(raw: Any) -> bool:
    """True when the raw value carried whitespace/nbsp/punctuation that
    normalization changed (so it's worth flagging for source cleanup)."""
    if raw is None:
        return False
    return str(raw) != normalize_text(raw)


def _capitalization_anomaly(name: str) -> bool:
    """Flag names that are ALL CAPS or all lowercase (likely source-entry noise).
    We do NOT auto-rewrite legal names — only flag them for human verification."""
    letters = [c for c in name if c.isalpha()]
    if len(letters) < 2:
        return False
    return name == name.upper() or name == name.lower()


def normalize_dob(value: Any) -> str | None:
    """Return an ISO ``YYYY-MM-DD`` date string when parseable, else the cleaned
    original (compound/partial dates like ``"?/?/1952"`` pass through)."""
    if value in (None, ""):
        return None
    if isinstance(value, (date, datetime)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    text = normalize_text(value)
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return text or None


# -- object-id / matter-number checks ---------------------------------------

_OBJECT_ID_RE = re.compile(r"^[0-9a-f]{24}$", re.IGNORECASE)


def looks_like_object_id(value: str | None) -> bool:
    """True for a Mongo-style 24-hex object id (Estateably's internal id)."""
    return bool(_OBJECT_ID_RE.match(str(value or "").strip()))


# -- validation → exception report ------------------------------------------

_NEAR_DUP_RATIO = 0.9


def _clean_name(raw: Any) -> tuple[str, list[str]]:
    """Normalize a party name and return (clean, issues)."""
    issues: list[str] = []
    clean = normalize_text(raw)
    if _had_formatting_noise(raw):
        issues.append("Name formatting anomaly (whitespace/punctuation)")
    if clean and _capitalization_anomaly(clean):
        issues.append("Unusual capitalization")
    if clean:
        issues.extend(_spelling_flags(clean))
    return clean, issues


def _party_issues(party: dict[str, Any]) -> list[str]:
    """Row-level issues derivable from a single party's fields."""
    if party.get("source_unavailable"):
        return ["Source unavailable (billing hold) — parties not retrievable"]
    issues = list(party.get("issues", []))
    number = party.get("matter_number")
    desc = party.get("description") or ""
    if not party.get("full_legal_name"):
        issues.append("Missing full legal name")
    if party.get("party_type") == "Individual" and not party.get("date_of_birth"):
        issues.append("Missing date of birth")
    if looks_like_object_id(number):
        issues.append("Object ID in matter number")
    if not number:
        issues.append("Matter number missing — enter at source")
    if number and desc and str(number) not in desc:
        issues.append("Matter number not found in description")
    return issues


def finalize(parties: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Attach ``exception_status`` to every party and return the exception rows
    (one per issue) plus summary stats. Adds cross-row checks: conflicting
    matter numbers and exact/near-duplicate party names."""
    # Cross-row: a human matter number that maps to >1 distinct source record.
    number_to_ids: dict[str, set[str]] = {}
    for p in parties:
        num = p.get("matter_number")
        if num:
            number_to_ids.setdefault(str(num), set()).add(str(p.get("source_record_id")))
    conflicting = {n for n, ids in number_to_ids.items() if len(ids) > 1}

    # Cross-row: exact + near-duplicate names (difflib, stdlib — no extra dep).
    names = sorted({p["full_legal_name"] for p in parties if p.get("full_legal_name")})
    near_dupes: set[str] = set()
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            if a == b:
                continue
            if abs(len(a) - len(b)) > 6:
                continue
            if difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() >= _NEAR_DUP_RATIO:
                near_dupes.add(a)
                near_dupes.add(b)

    exceptions: list[dict[str, Any]] = []
    missing_dob = incomplete_names = manual = 0
    for p in parties:
        issues = _party_issues(p)
        if p.get("matter_number") in conflicting:
            issues.append("Conflicting matter number (same number, different records)")
        if p.get("full_legal_name") in near_dupes:
            issues.append("Possible duplicate name (near-match to another party)")
        p["exception_status"] = "; ".join(issues)
        if any("date of birth" in i for i in issues):
            missing_dob += 1
        if any("legal name" in i for i in issues):
            incomplete_names += 1
        if issues:
            manual += 1
        for issue in issues:
            exceptions.append(
                {
                    "matter_number": p.get("matter_number"),
                    "source_record_id": p.get("source_record_id"),
                    "party": p.get("full_legal_name"),
                    "role": p.get("party_role"),
                    "issue": issue,
                }
            )

    summary = {
        "missing_dob": missing_dob,
        "incomplete_names": incomplete_names,
        "conflicting_matter_numbers": len(conflicting),
        "records_needing_review": manual,
    }
    return exceptions, summary


_ROSTER_INCOMPLETE = (
    "Party roster incomplete — verify settlor/trustee/beneficiary coverage "
    "(pending source data entry)"
)


def trust_roster_exceptions(parties: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One 'party roster incomplete' exception per in-scope Trust matter.

    Pass screened + excluded parties together so a trust whose only row was an
    excluded entity (e.g. the trust itself as "Client") is still flagged, per
    Aphrodite's request to flag every trust matter until source rosters are entered.
    """
    seen: dict[str, Any] = {}
    for p in parties:
        if (p.get("matter_type") or "").lower() == "trust":
            sid = str(p.get("source_record_id"))
            seen.setdefault(sid, p.get("matter_number"))
    return [
        {
            "matter_number": number,
            "source_record_id": sid,
            "party": "—",
            "role": "—",
            "issue": _ROSTER_INCOMPLETE,
        }
        for sid, number in seen.items()
    ]


__all__ = [
    "exclusion_reason",
    "is_inactive_status",
    "normalize_text",
    "normalize_dob",
    "looks_like_object_id",
    "finalize",
    "map_role",
    "is_excluded_party_name",
    "is_non_principal_role",
    "looks_like_entity",
    "entity_party_type",
    "trust_roster_exceptions",
    "SCREENING_ROLES",
    "_clean_name",
]
