"""Temporary A/B measurement runner (not a production route).

Drives the SAME matter-search pipeline against a candidate Bedrock model so we
can compare response time, cost, and answer quality across models for the
client's model-selection decision (Nicole's request). It is invoked out-of-band
(``aws lambda invoke``), never wired to API Gateway, and is created only when the
``ab_runner_enabled`` Terraform flag is on. It reuses the search execution role
and calls :func:`run_search` directly — read-only across every connector, with no
job-store or activity-log writes — and it does **not** change the deployed
default model (``_build_deps(model_id=...)`` overrides it per invocation only).

Event shape::

    {"query": "status of the Yurkiw estate",
     "model": "us.anthropic.claude-haiku-4-5",   # optional; omit → deployed default
     "full":  true}                               # optional; force full synthesis

Returns the measured wall time, the resolved model id, the token/cost usage, the
per-stage timings, quality signals (parse_ok / missing_sections / invalid vs
valid citation counts / dossier errors), and the full pipeline result so the
operator can read the section text for a qualitative review.
"""

from __future__ import annotations

from time import perf_counter
from typing import Any

from backend.api.handlers import _build_deps
from backend.api.pipeline import run_search
from services.logging import get_logger

logger = get_logger(__name__)


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    query = event.get("query")
    if not query:
        raise ValueError("ab_runner event requires a 'query'")
    model = event.get("model")
    full = bool(event.get("full", True))

    # Fresh deps per invocation so each run binds its own candidate model (no
    # cold-start dep cache — we want a clean model swap every time).
    deps = _build_deps(model_id=model)

    t0 = perf_counter()
    result = run_search(
        query,
        resolver=deps.resolver,
        dossier_builder=deps.dossier_builder,
        synthesizer=deps.synthesizer,
        quick=deps.quick,
        full=full,
        actor={"sub": "ab-runner"},
    )
    total_ms = round((perf_counter() - t0) * 1000, 1)

    usage = result.get("usage", {})
    answer = result.get("answer", {})
    summary = {
        "query": query,
        "model": usage.get("model") or model,
        "status": result.get("status"),
        "total_ms": total_ms,
        "timings": result.get("timings"),
        "usage": usage,
        "quality": {
            "parse_ok": answer.get("parse_ok"),
            "missing_sections": answer.get("missing_sections"),
            "invalid_citations": len(answer.get("invalid_citations") or []),
            "valid_citations": len(answer.get("citations") or []),
            "dossier_errors": len(result.get("dossier_errors") or []),
        },
    }
    logger.info("ab_runner complete", extra={"extra_fields": summary})
    return {"summary": summary, "result": result}
