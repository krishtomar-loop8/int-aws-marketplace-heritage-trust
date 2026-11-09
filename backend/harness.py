"""Connectivity test harness (D2).

Runs a minimal read-only probe against each source connector and returns a
structured pass/fail report. Probes are injectable for testing; the defaults
wire the real connectors against credentials in Secrets Manager.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

from services.logging import get_logger
from services.secrets import SecretsClient

logger = get_logger(__name__)


def _consume(result: Any) -> None:
    """Force one item from a lazy iterator so the underlying request executes."""
    if isinstance(result, Iterator):
        next(result, None)


def _default_probes(secrets: SecretsClient) -> dict[str, Callable[[], Any]]:
    """Cheapest read per source (no record IDs required)."""
    from backend.connectors.clio.connector import ClioConnector
    from backend.connectors.estateably.connector import EstateablyConnector
    from backend.connectors.graph.connector import GraphConnector
    from backend.connectors.quickbooks.connector import QuickBooksConnector

    return {
        "clio": lambda: _consume(ClioConnector(secrets).search_matters("test")),
        "estateably": lambda: _consume(EstateablyConnector(secrets).global_search("test")),
        "graph": lambda: _consume(GraphConnector(secrets).list_users(top=1)),
        "quickbooks": lambda: QuickBooksConnector(secrets).get_company_info(),
    }


def run_checks(
    probes: dict[str, Callable[[], Any]] | None = None,
    *,
    secrets: SecretsClient | None = None,
    enabled: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Run each probe, capturing per-source success without raising.

    ``enabled`` restricts the default probe set (ignored when ``probes`` is
    given explicitly) to connectors this deployment has turned on — a disabled
    connector's secret has its read grant withdrawn (Phase 2 modular
    architecture), so probing it would record a false "down" rather than
    skipping a check that was never meant to run.
    """
    if probes is None:
        probes = _default_probes(secrets or SecretsClient())
        if enabled is not None:
            probes = {name: fn for name, fn in probes.items() if name in enabled}

    sources: dict[str, Any] = {}
    overall = True
    for name, probe in probes.items():
        try:
            probe()
            sources[name] = {"ok": True, "detail": "ok"}
        except Exception as exc:
            sources[name] = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
            overall = False
        logger.info(
            "connectivity check",
            extra={"extra_fields": {"source": name, "ok": sources[name]["ok"]}},
        )
    return {"ok": overall, "sources": sources}


def run_check(name: str, secrets: SecretsClient | None = None) -> dict[str, Any]:
    """Run a single connector's cheapest read probe on demand.

    Used by the connector-onboarding routes to validate a freshly-saved
    credential immediately, instead of waiting for the next hourly probe.
    Raises ``ValueError`` for an unrecognized connector name.
    """
    secrets = secrets or SecretsClient()
    probes = _default_probes(secrets)
    if name not in probes:
        raise ValueError(f"unknown connector {name!r}")
    return run_checks({name: probes[name]}, secrets=secrets)["sources"][name]


def lambda_handler(event: dict[str, Any] | None = None, context: Any = None) -> dict[str, Any]:
    return run_checks()
