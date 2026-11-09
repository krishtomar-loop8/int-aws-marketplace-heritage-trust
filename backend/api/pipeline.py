"""Search pipeline — ties resolution → dossier → synthesis into one call.

This is the application core the API Lambda runs: given a plain-language query it
resolves the matter, and (when unambiguous) assembles the dossier and synthesizes
the source-cited summary, emitting a D7 audit event. It returns plain JSON-able
dicts so the Lambda handler just wraps them.

Resolver / dossier builder / synthesizer are injected, so this is unit-testable
with fakes and has no AWS/Bedrock/network dependency of its own.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from backend.search.intent import classify
from backend.search.orchestrator import DossierBuilder
from backend.search.quick_answers import QuickAnswerService
from backend.search.resolve import (
    MatterCandidate,
    MatterResolver,
    SourceRef,
    extract_term,
)
from backend.search.synthesize import MatterSynthesizer
from services import pricing
from services.logging import get_logger

if TYPE_CHECKING:
    from backend.api.activity import ActivityLog

logger = get_logger(__name__)

audit_logger = get_logger("aphrodite.audit")


def _candidate_json(c: MatterCandidate) -> dict[str, Any]:
    return {
        "name": c.name,
        "matter_type": c.matter_type,
        "score": c.score,
        "systems": sorted(c.systems),
        # qb_customer_id (the Clio client-contact id) must survive the round-trip
        # through the disambiguation UI: it drives the QuickBooks customer match
        # and the client-level Clio bill union. Dropping it here is why QuickBooks
        # reconciliation failed exactly on multi-matter clients (the only ones that
        # go through the disambiguation pick).
        "refs": [
            {
                "system": r.system,
                "id": r.id,
                "label": r.label,
                "qb_customer_id": r.qb_customer_id,
            }
            for r in c.refs
        ],
    }


def run_resolve(query: str, *, resolver: MatterResolver, scope: str = "all") -> dict[str, Any]:
    """Resolve a query to candidate matters (for the UI's disambiguation step)."""
    result = resolver.resolve(query, scope=scope)
    return {
        "query": result.query,
        "term": result.term,
        "matter_type": result.matter_type,
        "candidates": [_candidate_json(c) for c in result.candidates],
    }


def _emit_audit(actor: dict[str, Any] | None, event: str, **fields: Any) -> None:
    """D7 audit trail: who queried what, when, which sources, model, tokens.

    Matter identifiers are logged (the point of the audit); dossier *content* is
    never logged.
    """
    audit_logger.info(
        event,
        extra={"extra_fields": {"actor": (actor or {}).get("sub"), "event": event, **fields}},
    )


def _candidate_from_selection(selection: dict[str, Any]) -> MatterCandidate | None:
    """Rebuild a candidate the UI already resolved (from a disambiguation pick).

    The selection mirrors ``_candidate_json`` — ``{name, matter_type, refs}`` — so
    a follow-up can go straight to the dossier without re-running (and re-splitting)
    the resolver on the plain name, which would just re-disambiguate.
    """
    refs = [
        SourceRef(
            r["system"],
            str(r["id"]),
            r.get("label", ""),
            # Tolerate selections stored before the field existed (old sessions):
            # absent → None → the pre-existing name-fallback behavior.
            qb_customer_id=(str(r["qb_customer_id"]) if r.get("qb_customer_id") else None),
        )
        for r in selection.get("refs", [])
        if r.get("system") and r.get("id")
    ]
    if not refs:
        return None
    return MatterCandidate(
        name=selection.get("name", refs[0].label),
        refs=refs,
        matter_type=selection.get("matter_type"),
    )


def run_search(
    query: str,
    *,
    resolver: MatterResolver,
    dossier_builder: DossierBuilder,
    synthesizer: MatterSynthesizer,
    actor: dict[str, Any] | None = None,
    selection: dict[str, Any] | None = None,
    quick: QuickAnswerService | None = None,
    full: bool = False,
    scope: str = "all",
    on_phase: Callable[[str], None] | None = None,
    activity: ActivityLog | None = None,
    remaining_ms: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Resolve → (dossier → synthesis) for an unambiguous match.

    When ``selection`` is supplied (the user picked a candidate from a prior
    disambiguation), it names the matter directly and resolution is skipped.

    Tier 0: when ``quick`` is supplied and ``full`` is false, a narrow intent
    (e.g. "trust balance", "who are the beneficiaries") is answered directly from
    the connectors with no LLM. Status/overview queries and any non-match fall
    through to the full synthesis unchanged. ``full=True`` forces synthesis.

    Returns one of:
      - ``{"status": "not_found"}`` — no candidate matched;
      - ``{"status": "no_sources"}`` — every resolution source (Estateably, Clio)
        is disabled for this deployment, so no search could run at all;
      - ``{"status": "disambiguation", "candidates": [...]}`` — multiple matches;
      - ``{"status": "answer", "matter": ..., "answer": {...}}`` — Tier-0 quick answer;
      - ``{"status": "ok", "matter": ..., "answer": ..., "usage": ...}``.
    """
    phase = on_phase or (lambda _p: None)  # progress updates for the UI (no-op if unset)

    resolve_ms = 0.0  # 0 when a selection skips resolution
    candidate = _candidate_from_selection(selection) if selection else None
    if candidate is not None:
        term = extract_term(query)[0]
    else:
        phase("Finding the matter")
        t_resolve = time.perf_counter()
        resolution = resolver.resolve(query, scope=scope)
        resolve_ms = round((time.perf_counter() - t_resolve) * 1000, 1)

        if not resolution.candidates:
            # A fake/stub resolver with no `sources_available` attribute is assumed
            # to have sources — fail open, same posture as every other Phase 2
            # capability default (never hide/misreport on an unrelated test double).
            if not getattr(resolver, "sources_available", True):
                _emit_audit(actor, "search.no_sources", query_term=resolution.term)
                if activity is not None:
                    activity.record(
                        actor,
                        "matter.search",
                        f"Searched for “{resolution.term}” — no sources are enabled "
                        "for this deployment",
                        target=resolution.term,
                        detail={"outcome": "no_sources"},
                    )
                return {"status": "no_sources", "query": query, "term": resolution.term}
            _emit_audit(actor, "search.not_found", query_term=resolution.term)
            if activity is not None:
                activity.record(
                    actor,
                    "matter.search",
                    f"Searched for “{resolution.term}” — no matching matter found",
                    target=resolution.term,
                    detail={"outcome": "not_found"},
                )
            return {"status": "not_found", "query": query, "term": resolution.term}

        if resolution.resolved is None:
            _emit_audit(actor, "search.disambiguation", candidates=len(resolution.candidates))
            return {
                "status": "disambiguation",
                "query": query,
                "candidates": [_candidate_json(c) for c in resolution.candidates],
            }

        candidate = resolution.resolved
        term = resolution.term

    matter = {
        "name": candidate.name,
        "type": candidate.matter_type,
        "refs": _candidate_json(candidate)["refs"],
    }

    # Tier 0: deterministic quick answer for narrow intents (no LLM). Any
    # status/overview query, non-match, or handler decline falls through to the
    # full synthesis below unchanged. ``full=True`` skips this entirely.
    if quick is not None and not full:
        intent = classify(query)
        if intent:
            qa = quick.answer(intent, candidate, term=term, query=query)
            if qa is not None:
                _emit_audit(
                    actor,
                    "search.quick_answer",
                    matter_name=candidate.name,
                    intent=intent,
                    systems=sorted(candidate.systems),
                )
                if activity is not None:
                    activity.record(
                        actor,
                        "matter.search",
                        f"Looked up “{candidate.name}” (quick answer)",
                        target=candidate.name,
                        sources=sorted(candidate.systems),
                        detail={"intent": intent, "mode": "quick_answer"},
                    )
                return {
                    "status": "answer",
                    "matter": matter,
                    "quick": {"intent": qa.intent, "text": qa.text, "citations": qa.citations},
                }

    phase("Reading Estateably, Clio and Outlook")
    t_build = time.perf_counter()
    dossier = dossier_builder.build(candidate, correspondence_term=term)
    build_ms = round((time.perf_counter() - t_build) * 1000, 1)
    phase("Drafting the source-cited summary")
    t_synth = time.perf_counter()
    synthesis = synthesizer.synthesize(dossier, query, remaining_ms=remaining_ms)
    synth_ms = round((time.perf_counter() - t_synth) * 1000, 1)

    # Per-search cost from real token usage (None when the model is unpriced).
    cost_usd = pricing.cost_usd(
        synthesis.model_id,
        synthesis.input_tokens,
        synthesis.output_tokens,
        synthesis.cache_read_tokens,
        synthesis.cache_write_tokens,
    )

    # "Insufficient source information" is a FACT about the dossier, not a model
    # opinion — the model set its flag on nearly every run (any one unconfirmed
    # section), so the banner had stopped meaning anything. Derive it from failed
    # pulls of the core sources; a QuickBooks outage counts only when the matter
    # actually had a QuickBooks customer to reconcile (a Clio client-contact id).
    qb_expected = any(getattr(r, "qb_customer_id", None) for r in candidate.refs)
    insufficient_reasons = [
        e
        for e in dossier.errors
        if e.split(":", 1)[0].strip().lower() in ("estateably", "clio", "outlook")
        or ("quickbooks" in e.lower() and qb_expected)
    ]

    _emit_audit(
        actor,
        "search.completed",
        matter_name=candidate.name,
        systems=sorted(candidate.systems),
        model=synthesis.model_id,
        input_tokens=synthesis.input_tokens,
        output_tokens=synthesis.output_tokens,
        cache_read_tokens=synthesis.cache_read_tokens,
        cache_write_tokens=synthesis.cache_write_tokens,
        cost_usd=cost_usd,
        dossier_errors=len(dossier.errors),
        invalid_citations=len(synthesis.invalid_citations),
        missing_sections=len(synthesis.missing_sections),
        parse_ok=synthesis.parse_ok,
        stop_reason=synthesis.stop_reason,
        retried=synthesis.retried,
        # The model's own insufficient_info self-report, kept for monitoring only —
        # the answer payload carries the deterministic derivation instead.
        model_insufficient=synthesis.insufficient_info,
        # Latency instrumentation: total = resolve + build (max of per-source) + synth.
        resolve_ms=resolve_ms,
        build_ms=build_ms,
        synth_ms=synth_ms,
        source_ms=dossier.timings,
    )
    if activity is not None:
        activity.record(
            actor,
            "matter.search",
            f"Ran a full matter search on “{candidate.name}”",
            target=candidate.name,
            sources=sorted(candidate.systems),
            detail={"mode": "full", "model": synthesis.model_id},
        )

    return {
        "status": "ok",
        "matter": matter,
        "answer": {
            "sections": synthesis.sections,
            "citations": synthesis.citations,
            "invalid_citations": synthesis.invalid_citations,
            # Deterministic, not the model's echo: the dossier list carries factual
            # notes (reviewed-to cutoff, off-matter exclusion counts, QuickBooks
            # no-match) that the model was paraphrasing away or dropping.
            "sources_reviewed": dossier.sources_reviewed,
            "insufficient_info": bool(insufficient_reasons),
            "insufficient_reasons": insufficient_reasons,
            "missing_sections": synthesis.missing_sections,
            "parse_ok": synthesis.parse_ok,
        },
        "dossier_errors": dossier.errors,
        "usage": {
            "model": synthesis.model_id,
            "input_tokens": synthesis.input_tokens,
            "output_tokens": synthesis.output_tokens,
            "cache_read_tokens": synthesis.cache_read_tokens,
            "cache_write_tokens": synthesis.cache_write_tokens,
            "cost_usd": cost_usd,
        },
        "timings": {
            "resolve_ms": resolve_ms,
            "build_ms": build_ms,
            "synth_ms": synth_ms,
            "source_ms": dossier.timings,
        },
    }
