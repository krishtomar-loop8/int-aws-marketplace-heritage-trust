"""Deterministic intent classification for Tier-0 quick answers (no LLM).

Maps a plain-language query to one of a small set of narrow intents that can be
answered with 1-2 direct connector calls. Returns ``None`` for anything that is
not a confident narrow match — including comprehensive "status/overview" asks —
so those fall through to the full LLM synthesis (Tier 2) unchanged.

Precision over recall: a wrong quick answer is worse than a slightly slower full
one, so the rules are intentionally tight and overview phrasing always wins.
"""

from __future__ import annotations

import re

# Strong, unambiguous tax signals. Checked FIRST so "filing status" / "tax status"
# classify as a tax question rather than tripping the "status" overview guard.
_TAX = (
    "noa",
    "notice of assessment",
    "clearance",
    "tax return",
    "tax filing",
    "tax status",
    "filing status",
)
_T13_RE = re.compile(r"\bt[13]\b")  # terminal T1 / T3 return

# Single-fact "matter details" about the deceased/matter, from the Estateably
# client-intake record. Checked before the "status" guard so "marital status"
# isn't swallowed by it.
_PROFILE = (
    "date of birth",
    "born",
    "date of death",
    "died",
    "passed away",
    "marital",
    "province",
    "jurisdiction",
    "domicile",
)
_DOBDOD_RE = re.compile(r"\bdo[bd]\b")  # dob / dod
_DEATH_RE = re.compile(r"\bdie[ds]?\b")  # die / died / dies

# Phrasings that signal the caller wants the comprehensive status file. These
# ALWAYS route to Tier 2 (full synthesis) — so "what's the status of X?" never
# becomes a quick answer. (Checked after the strong tax signals above.)
_OVERVIEW = (
    "status",
    "summary",
    "summarize",
    "overview",
    "update",
    "full picture",
    "everything",
    "where are things",
    "where things",
    "how are things",
    "how's things",
    "hows things",
    "tell me about",
    "brief me",
    "rundown",
    "run down",
    "catch me up",
    "ready for distribution",  # a gate assessment — needs the full synthesis
)

# Ordered narrow-intent rules: (intent, keywords). First match wins; keep tight.
_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("balance", ("balance", "how much", "funds", "cash on hand", "money in")),
    (
        "kyc",
        (
            "kyc",
            "know your client",
            "know-your-client",
            "intake",
            "id on file",
            "risk rating",
            "pep",
        ),
    ),
    (
        "deadlines",
        ("deadline", "due date", "upcoming", "diariz", "tickler", "what's due", "whats due"),
    ),
    ("parties", ("beneficiar", "executor", "trustee", "who are the", "who is the", "settlor")),
    ("documents", ("document", "docs", "on file", "paperwork", "what files", "which files")),
    (
        "correspondence",
        ("email", "correspondence", "communications", "who emailed", "recent activity"),
    ),
    (
        "bills",
        ("invoice", "bill", "amount owing", "outstanding balance", "accounts payable", "unpaid"),
    ),
)


def _norm(query: str) -> str:
    return re.sub(r"\s+", " ", query.lower()).strip()


def classify(query: str) -> str | None:
    """Return a Tier-0 intent name, or ``None`` to fall through to Tier 2."""
    q = _norm(query)
    if not q:
        return None
    if _T13_RE.search(q) or any(t in q for t in _TAX):
        return "tax_status"
    if _DOBDOD_RE.search(q) or _DEATH_RE.search(q) or any(p in q for p in _PROFILE):
        return "profile"
    if any(word in q for word in _OVERVIEW):
        return None  # comprehensive ask → full synthesis
    for intent, keywords in _RULES:
        if any(k in q for k in keywords):
            return intent
    return None
