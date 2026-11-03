"""Matter resolution — plain-language query to per-system matter identifiers.

A staff query like *"What's the status on the Smith estate?"* must be turned into
concrete record ids in each source system before the fan-out can read anything.
This module extracts the search term, queries the source systems' own search
endpoints, and correlates the hits into candidate matters.

Slice 1 resolves against **Estateably** (estates) and **Clio** (matters); Slice 2
adds SharePoint site/folder and correspondence-mailbox resolution. Connectors are
injected (and optional) so this is unit-testable without AWS or network, and so a
source that is unavailable simply contributes no candidates.

If exactly one candidate is found the query is considered *resolved*; multiple
candidates are returned for the UI to disambiguate.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from services.logging import get_logger

logger = get_logger(__name__)

# Filler tokens stripped when extracting the search term from a natural question.
_STOPWORDS = frozenset(
    {
        "a", "an", "the", "whats", "what", "s", "is", "status", "on", "of",
        "for", "me", "please", "give", "tell", "show", "any", "update", "updates",
        "matter", "file", "client", "case", "how", "are", "things", "going", "with",
        "current", "summary", "get", "check", "whos", "who",
    }
)  # fmt: skip
# Tokens that hint at the matter type (and are not part of the name).
_TYPE_TOKENS = {"estate": "estate", "trust": "trust", "poa": "poa"}

_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def extract_term(query: str) -> tuple[str, str | None]:
    """Return ``(search_term, matter_type)`` extracted from a natural query.

    Filler words and matter-type words are stripped; the remaining tokens (in
    original order) form the search term. ``matter_type`` is ``estate``/``trust``/
    ``poa`` when such a word appears, else ``None``.
    """
    matter_type: str | None = None
    kept: list[str] = []
    for tok in _tokens(query):
        if tok in _TYPE_TOKENS:
            matter_type = _TYPE_TOKENS[tok]
            continue
        if tok in _STOPWORDS:
            continue
        kept.append(tok)
    return " ".join(kept), matter_type


def _normalize(name: str) -> frozenset[str]:
    """Significant-token set of a name, for cross-system correlation."""
    return frozenset(t for t in _tokens(name) if t not in _STOPWORDS and t not in _TYPE_TOKENS)


def _tidy_name(name: str) -> str:
    """Normalize display spacing in a source label: give a separator hyphen a space
    on both sides (e.g. "Estate Administration -Estate of X" → "… - Estate of X")
    without touching a hyphenated surname ("Smith-Jones"), and collapse whitespace."""
    s = re.sub(r" -(?=\S)", " - ", name)  # space before the hyphen, none after
    s = re.sub(r"(?<=\S)- ", " - ", s)  # none before, space after
    return re.sub(r"\s{2,}", " ", s).strip()


# A token appearing in this many distinct refs (in a large enough result set) is
# treated as too common to be a reliable merge key — almost always a shared given
# name (e.g. "david") or generic word rather than a matter identifier.
_MIN_REFS_FOR_COMMON = 5
_COMMON_TOKEN_MIN_REFS = 3
# A candidate scoring below this fraction of the top candidate's score is a weak
# partial (e.g. shares only a common first name) and is dropped so a clear leader
# auto-resolves; matches within the fraction are kept and disambiguate.
_RESOLVE_SCORE_FRACTION = 0.5


def _common_tokens(refs: list[SourceRef]) -> set[str]:
    """Tokens too common across the result set to signal the same matter.

    Only computed for larger result sets; small sets keep the simple
    any-shared-token merge. Because Estateably refs are de-duped by case id, two
    refs sharing a token here are distinct cases — so a token spanning several of
    them is a common name/word, not a matter identifier, and must not merge them.
    """
    if len(refs) < _MIN_REFS_FOR_COMMON:
        return set()
    counts: dict[str, int] = {}
    for ref in refs:
        for tok in _normalize(ref.label):
            counts[tok] = counts.get(tok, 0) + 1
    return {tok for tok, n in counts.items() if n >= _COMMON_TOKEN_MIN_REFS}


@dataclass(frozen=True)
class SourceRef:
    """A matched record in one source system."""

    system: str  # "estateably" | "clio"
    id: str
    label: str
    # Clio only: the matter's client-contact id. Clio's QuickBooks Online sync
    # creates a QBO customer per CONTACT (the client), not per matter, and stamps
    # this contact id into the customer DisplayName as "(Clio <id>)". It's how a
    # matter is matched to its QuickBooks customer (the matter id is NOT in QBO).
    qb_customer_id: str | None = None


@dataclass
class MatterCandidate:
    """One candidate matter, possibly backed by several source records."""

    name: str
    refs: list[SourceRef] = field(default_factory=list)
    matter_type: str | None = None
    score: float = 0.0

    @property
    def systems(self) -> set[str]:
        return {r.system for r in self.refs}


@dataclass
class ResolutionResult:
    query: str
    term: str
    matter_type: str | None
    candidates: list[MatterCandidate]

    @property
    def resolved(self) -> MatterCandidate | None:
        """The single unambiguous candidate, or ``None`` if 0 or >1."""
        return self.candidates[0] if len(self.candidates) == 1 else None


class MatterResolver:
    """Resolve a plain-language query to candidate matters across sources."""

    def __init__(self, *, estateably=None, clio=None) -> None:
        self._estateably = estateably
        self._clio = clio

    def resolve(self, query: str, *, limit: int = 10, scope: str = "all") -> ResolutionResult:
        term, matter_type = extract_term(query)
        # Estateably and Clio searches are independent — run them concurrently, then
        # concatenate in a stable order (estateably first) so correlation/ref-order
        # is unchanged. Each _search_* swallows its own errors (returns []).
        # scope="clio" skips the Estateably search entirely (staff searching an old
        # Clio + SharePoint matter with no Estateably record): with Estateably out,
        # no multi-system corroboration bonus inflates a similarly-named current
        # matter, so the Clio-only match clears the relative cutoff below and surfaces.
        tasks: list[tuple[str, Callable[[], list[SourceRef]]]] = []
        if self._estateably is not None and scope != "clio":
            tasks.append(("estateably", lambda: self._search_estateably(term)))
        if self._clio is not None:
            tasks.append(("clio", lambda: self._search_clio(term)))
        results: dict[str, list[SourceRef]] = {}
        if tasks:
            with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
                futures = {name: ex.submit(fn) for name, fn in tasks}
                results = {name: f.result() for name, f in futures.items()}
        refs: list[SourceRef] = results.get("estateably", []) + results.get("clio", [])

        candidates = self._correlate(refs, term)
        # Drop noise so a clear match resolves instead of forcing disambiguation:
        # (a) zero-overlap hits (full-text matches on unrelated fields, or same-
        #     family sub-matters whose label lacks the client name); and
        # (b) weak partials far below a clear leader (e.g. hits sharing only a
        #     common first name with the term). The cutoff is relative to the top
        #     score, so genuinely close matches are all kept and still disambiguate.
        top = max((c.score for c in candidates), default=0.0)
        if top > 0:
            cutoff = top * _RESOLVE_SCORE_FRACTION
            candidates = [c for c in candidates if c.score > 0 and c.score >= cutoff]
        candidates.sort(key=lambda c: (c.score, len(c.refs)), reverse=True)
        for c in candidates:
            c.matter_type = c.matter_type or matter_type
        return ResolutionResult(
            query=query, term=term, matter_type=matter_type, candidates=candidates[:limit]
        )

    # -- per-source search (best-effort; a failing source is skipped) --------

    def _search_estateably(self, term: str) -> list[SourceRef]:
        try:
            hits = self._estateably.global_search(term)
        except Exception as exc:  # noqa: BLE001 - one bad source must not kill resolution
            logger.warning("estateably search failed", extra={"extra_fields": {"error": str(exc)}})
            return []
        refs: list[SourceRef] = []
        seen: set[str] = set()
        for h in hits:
            if not isinstance(h, dict):
                continue
            attrs = h.get("attributes", {})
            fields = attrs.get("fields") if isinstance(attrs.get("fields"), dict) else {}
            # A globalSearch hit may be the case itself OR a related entity within
            # it (a contact/beneficiary), so key on the *case* it belongs to:
            # ``fields.caseId`` + ``fields.caseName``. ``entityId`` is the matched
            # entity id (e.g. a contact) — NOT the case — and preferring it resolves
            # to the wrong estate (and, since the case's own summary is keyed by
            # caseId, to an id that 402/404s). Several hits point at one case, so
            # dedupe by case id. Fall back to entity/hit id + value for other shapes.
            hid = fields.get("caseId") or attrs.get("entityId") or h.get("id")
            label = (
                fields.get("caseName")
                or attrs.get("value")
                or attrs.get("title")
                or attrs.get("name")
                or ""
            )
            if hid and label and str(hid) not in seen:
                seen.add(str(hid))
                refs.append(SourceRef("estateably", str(hid), _tidy_name(str(label))))
        return refs

    def _search_clio(self, term: str) -> list[SourceRef]:
        try:
            # client{id} rides along free — it's the matter's client-contact id, which
            # Clio's QuickBooks sync stamps into the QBO customer DisplayName (the QB
            # reconciliation matches on it; see SourceRef.qb_customer_id).
            hits = self._clio.search_matters(
                term, fields="id,display_number,description,client{id}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("clio search failed", extra={"extra_fields": {"error": str(exc)}})
            return []
        refs = []
        for h in hits:
            if not isinstance(h, dict):
                continue
            hid = h.get("id")
            label = h.get("description") or h.get("display_number") or ""
            client = h.get("client") if isinstance(h.get("client"), dict) else {}
            client_id = client.get("id")
            if hid and label:
                refs.append(
                    SourceRef(
                        "clio",
                        str(hid),
                        _tidy_name(str(label)),
                        qb_customer_id=str(client_id) if client_id else None,
                    )
                )
        return refs

    # -- correlation --------------------------------------------------------

    def _correlate(self, refs: list[SourceRef], term: str) -> list[MatterCandidate]:
        """Group refs that share a *distinctive* token into matter candidates.

        Two refs are treated as the same matter only when they share a token that
        isn't ubiquitous across the result set. A token shared by many otherwise
        unrelated hits — e.g. a common given name like "david" spanning David
        Yurkiw, David Malek, David Sparks — is not a reliable merge signal: keying
        on it collapses every "david" case into one candidate, and the dossier
        then reads whichever id happens to be last. Such common tokens are dropped
        from the *merge key* (small result sets keep the simple any-shared-token
        behaviour). Scoring still uses the full label tokens so a genuine match
        (e.g. estate + Clio matter sharing the surname) still ranks highest.
        """
        term_tokens = _normalize(term)
        common = _common_tokens(refs)
        candidates: list[MatterCandidate] = []
        tokens: list[set[str]] = []  # full label tokens per candidate (for scoring)
        keys: list[set[str]] = []  # distinctive tokens per candidate (for merging)

        for ref in refs:
            toks = _normalize(ref.label)
            mkey = toks - common
            placed = False
            for i, existing in enumerate(keys):
                if mkey & existing:  # shared *distinctive* token → same matter
                    candidates[i].refs.append(ref)
                    tokens[i] |= toks
                    keys[i] |= mkey
                    if len(ref.label) > len(candidates[i].name):
                        candidates[i].name = ref.label
                    placed = True
                    break
            if not placed:
                candidates.append(MatterCandidate(name=ref.label, refs=[ref]))
                tokens.append(set(toks))
                keys.append(set(mkey))

        # Score: overlap with the query term + a bump for multi-system corroboration.
        for cand, toks in zip(candidates, tokens, strict=True):
            overlap = len(term_tokens & toks) / len(term_tokens) if term_tokens else 0.0
            cand.score = round(overlap + 0.25 * (len(cand.systems) - 1), 3)
        return candidates
