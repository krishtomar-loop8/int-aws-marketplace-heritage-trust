"""Synthesis — turn a matter dossier into the source-cited status summary.

Builds the prompt from the dossier + the staff question, calls the model with the
static operational framework as a **prompt-cached** system prefix (cost lever),
parses the structured JSON answer, and derives the citation list from the inline
"(system:record_id)" markers the model wrote — keeping only those that correspond
to a real record in the dossier (grounding check).

The model is accessed through :class:`services.llm.BedrockLLM` (config-driven, so
Sonnet 4.6 by default). This module never talks to AWS directly, so it is
unit-testable with a fake LLM.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from backend.search.framework import (
    DEFAULT_ORG_NAME,
    DEFAULT_ORG_OFFICE_ADDRESS,
    SECTION_KEYS,
    render_framework,
)
from backend.search.orchestrator import DossierItem, MatterDossier
from services.logging import get_logger

logger = get_logger(__name__)

# The 12-section answer plus its inline citation markers must fit in one response;
# too small truncates the JSON (unparseable → empty sections). Generous headroom
# so a rich matter never truncates — accuracy over latency, per the client's
# explicit call: search may take as long as it needs if it improves the output.
# The model itself allows far more (128k, confirmed live against the deployed
# Bedrock inference profile) — the real ceiling is the search Lambda's fixed 900s
# timeout (AWS's own hard maximum, not ours to raise) combined with the observed
# generation rate (~28-33ms/output token on a real large-matter run): a single
# call near this cap already uses ~10-12 minutes of the 900s budget, which is why
# this isn't pushed further — a bigger number would risk the Lambda's own timeout
# killing the invocation mid-generation, which is worse than a clean truncation.
# The read-timeout config in services.llm is sized to match (see handlers.py).
DEFAULT_MAX_TOKENS = 22000

# On the rare matter that still overruns the budget (stop_reason "max_tokens" or an
# unparseable reply), synthesis retries once at up to this many tokens. Bounded by
# the same Lambda-timeout arithmetic as DEFAULT_MAX_TOKENS above.
RETRY_MAX_TOKENS = 26000

# A truncation retry may generate for a while. Only retry when at least this much
# Lambda time remains, so the second call can't run past the function timeout and
# strand the job as perpetually "pending" — scaled from the prior tuning
# (470_000ms at a 24000-token retry ceiling) to the new RETRY_MAX_TOKENS.
RETRY_TIME_BUDGET_MS = 510_000

# Inline citation markers: any parenthesised group carrying at least one
# "system:" token. Kept loose (mirrors the frontend CITE_MARKER_RE in
# frontend/src/lib/citations.ts): the model drops the "system:" prefix on
# continuation ids, truncates long ids with "…", and mixes systems within one
# marker — matching the whole group means such markers are handled, and
# _marker_refs() salvages the usable ids.
_SYSTEMS = ("estateably", "clio", "quickbooks", "graph", "sharepoint", "outlook")
_SYS_ALT = "|".join(_SYSTEMS)
CITE_MARKER_RE = re.compile(rf"\([^)]*?(?:{_SYS_ALT})\s*:[^)]*\)", re.IGNORECASE)
_SYS_ONLY_RE = re.compile(rf"^(?:{_SYS_ALT})$", re.IGNORECASE)
_ID_RE = re.compile(r"^[^\s,;)]+")


@dataclass
class SynthesisResult:
    sections: dict[str, str]
    citations: list[dict[str, Any]]
    invalid_citations: list[dict[str, Any]]  # cited but not found in the dossier
    sources_reviewed: list[str]
    insufficient_info: bool
    model_id: str
    input_tokens: int
    output_tokens: int
    raw_text: str
    parse_ok: bool = True
    missing_sections: list[str] = field(default_factory=list)
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    stop_reason: str = ""
    retried: bool = False


def _parse_json(text: str) -> tuple[dict[str, Any], bool]:
    """Tolerantly extract the JSON object from the model's reply."""
    try:
        return json.loads(text), True
    except (json.JSONDecodeError, TypeError):
        pass
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start : end + 1]), True
        except json.JSONDecodeError:
            pass
    return {}, False


def _marker_refs(marker: str) -> list[tuple[str, str]]:
    """Pull the (system, id) refs out of one "(system:id; id2; other:id3)" marker.

    Mirrors the frontend ``markerRefs`` (frontend/src/lib/citations.ts): a bare
    continuation id inherits the last-named system; the id is the first token
    (the model sometimes trails a date/note inside the parens), with trailing
    sentence punctuation trimmed.
    """
    inner = marker.strip()[1:-1]  # drop the surrounding parens
    refs: list[tuple[str, str]] = []
    last_system: str | None = None
    for raw in inner.split(";"):
        part = raw.strip()
        if not part:
            continue
        rest = part
        colon = part.find(":")
        if colon >= 0 and _SYS_ONLY_RE.match(part[:colon].strip()):
            system: str | None = part[:colon].strip().lower()
            rest = part[colon + 1 :]
            last_system = system
        else:
            system = last_system  # bare continuation id — inherit the marker's system
        if not system:
            continue
        m = _ID_RE.match(rest.strip())
        rid = (m.group(0) if m else "").rstrip(".,;")
        if rid:
            refs.append((system, rid))
    return refs


def _build_citations(
    sections: dict[str, Any], dossier: MatterDossier
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Derive the grounded citations list from the inline markers in the prose.

    The model emits "(system:record_id)" markers in the section text (and no
    separate array — that repeated every id and bloated the output past the token
    cap). We scan the prose for markers in section order, keep only refs that
    resolve to a real dossier record (grounding), de-duplicate, and enrich each
    with the record's ``url`` and a short ``note`` (the dossier item's summary) so
    the SPA renders numbered references exactly as before. Markers that ground to
    nothing are returned as ``invalid`` for the audit count.
    """
    item_by_ref: dict[tuple[str, str], DossierItem] = {}
    for sec in dossier.sections.values():
        for item in sec.items:
            if not item.record_id or item.record_id == "-":
                continue
            item_by_ref.setdefault((item.system, item.record_id), item)

    valid: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for key, _ in SECTION_KEYS:
        text = sections.get(key)
        if not isinstance(text, str):
            continue
        for marker in CITE_MARKER_RE.finditer(text):
            for system, rid in _marker_refs(marker.group(0)):
                ref = (system, rid)
                if ref in seen:
                    continue
                seen.add(ref)
                item = item_by_ref.get(ref)
                if item is None:
                    invalid.append({"system": system, "record_id": rid})
                    continue
                cite: dict[str, Any] = {"system": system, "record_id": rid}
                if item.summary:
                    cite["note"] = item.summary[:200]
                url = item.data.get("url") if item.data else None
                if url:
                    cite["url"] = url
                # In-app evidence: the masked, budget-truncated email body excerpt lets a
                # reader verify a cited email inside the report — the Outlook deep-link only
                # opens for someone with access to that message's mailbox. Only
                # correspondence items carry data["body"]; ship the date alongside it so the
                # evidence disclosure can show when the email was sent.
                body = item.data.get("body") if item.data else None
                if body:
                    cite["excerpt"] = body
                    if item.date:
                        cite["date"] = item.date
                valid.append(cite)
    return valid, invalid


def _normalize_sections(sections: Any) -> dict[str, str]:
    """Coerce model-produced section values to strings (the response contract).

    ``sections`` is declared ``dict[str, str]``, but the model sometimes emits a
    section as a JSON array or object — most often ``sources_reviewed``, which is
    also a top-level array key, so the model conflates the two. The SPA renders
    each value as Markdown text (``text.split(...)``), so a non-string value throws
    at render and, with no error boundary, blanks the whole page. A list becomes
    bullet lines; any other non-string is stringified; ``None`` is dropped.
    """
    if not isinstance(sections, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in sections.items():
        if isinstance(value, str):
            out[str(key)] = value
        elif isinstance(value, list):
            out[str(key)] = "\n".join(f"- {str(v).strip()}" for v in value if str(v).strip())
        elif value is not None:
            out[str(key)] = str(value)
    return out


def _as_str_list(value: Any) -> list[str]:
    """Coerce a model-produced value to ``list[str]`` (the SPA ``.map()``s it)."""
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str) and value.strip():
        return [value]
    return []


class MatterSynthesizer:
    """Produce the 12-section, source-cited summary from a dossier."""

    def __init__(
        self,
        llm: Any,
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        org_name: str = DEFAULT_ORG_NAME,
        office_address: str = DEFAULT_ORG_OFFICE_ADDRESS,
    ) -> None:
        self._llm = llm
        self._max_tokens = max_tokens
        self._framework = render_framework(org_name, office_address)

    def synthesize(
        self,
        dossier: MatterDossier,
        query: str,
        *,
        remaining_ms: Callable[[], float] | None = None,
    ) -> SynthesisResult:
        prompt = self._build_prompt(dossier, query)
        result = self._llm.synthesize(
            prompt, cache_system=self._framework, max_tokens=self._max_tokens
        )
        parsed, parse_ok = _parse_json(result.text)

        # Large, record-heavy matters can overrun the output budget: the JSON is
        # cut off mid-object and won't parse (empty sections). Retry once with a
        # bigger budget when the model hit the token ceiling or the reply didn't
        # parse — this is the backstop; DEFAULT_MAX_TOKENS should cover almost all.
        # Only retry when enough Lambda time remains for another full generation, so
        # the second call can't run past the function timeout and strand the job.
        retried = False
        retry_tokens = min(self._max_tokens * 2, RETRY_MAX_TOKENS)
        wants_retry = result.stop_reason == "max_tokens" or not parse_ok
        have_budget = remaining_ms is None or remaining_ms() >= RETRY_TIME_BUDGET_MS
        if wants_retry and retry_tokens > self._max_tokens and have_budget:
            logger.warning(
                "synthesis truncated; retrying with a larger token budget",
                extra={
                    "extra_fields": {
                        "stop_reason": result.stop_reason,
                        "parse_ok": parse_ok,
                        "retry_max_tokens": retry_tokens,
                    }
                },
            )
            result = self._llm.synthesize(
                prompt, cache_system=self._framework, max_tokens=retry_tokens
            )
            parsed, parse_ok = _parse_json(result.text)
            retried = True
        elif wants_retry and not have_budget:
            logger.warning(
                "synthesis truncated but insufficient time budget to retry",
                extra={"extra_fields": {"stop_reason": result.stop_reason, "parse_ok": parse_ok}},
            )

        # The model reply may parse to a non-dict (e.g. a bare array); guard once so
        # every field read below is safe, then coerce the fields the SPA renders.
        parsed_obj = parsed if isinstance(parsed, dict) else {}
        sections = _normalize_sections(parsed_obj.get("sections"))
        valid, invalid = _build_citations(sections, dossier)
        if invalid:
            logger.warning(
                "synthesis produced ungrounded citations",
                extra={"extra_fields": {"count": len(invalid)}},
            )
        missing = [k for k, _ in SECTION_KEYS if k not in sections] if parse_ok else []

        return SynthesisResult(
            sections=sections,
            citations=valid,
            invalid_citations=invalid,
            sources_reviewed=_as_str_list(parsed_obj.get("sources_reviewed")),
            insufficient_info=bool(parsed_obj.get("insufficient_info", False)),
            model_id=result.model_id,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            raw_text=result.text,
            parse_ok=parse_ok,
            missing_sections=missing,
            cache_read_tokens=result.cache_read_tokens,
            cache_write_tokens=result.cache_write_tokens,
            stop_reason=result.stop_reason,
            retried=retried,
        )

    def _build_prompt(self, dossier: MatterDossier, query: str) -> str:
        return (
            f"Staff question: {query}\n\n"
            f"Matter: {dossier.matter_name} (type: {dossier.matter_type or 'unknown'})\n\n"
            "Matter dossier (the ONLY source data available; cite records from it):\n"
            # Compact separators (no indentation) — the dossier is machine data, so
            # this drops pure-whitespace tokens with zero information loss.
            f"{json.dumps(dossier.as_dict(), separators=(',', ':'), default=str)}\n\n"
            "Produce the status summary as the JSON object specified in the system "
            "instructions. Cite only records present in the dossier above."
        )
