"""Lambda entry point for the matter-search API.

A single function serves three HTTP routes (behind API Gateway's Cognito JWT
authorizer) plus an async worker path:

- ``POST /resolve``          → candidate matters for disambiguation (synchronous)
- ``POST /search``           → create a job, self-invoke the worker async, return
                               the job id (202) — search can exceed API GW's 29s
- ``GET  /search/{jobId}``   → poll job status / result
- worker event (``{"worker": true, ...}``) → runs the pipeline, stores the result

Dependencies (connectors, LLM, job store, self-invoke) are built once per cold
start and cached; tests inject a fake ``Deps`` via :data:`_DEPS`, so the routing
and worker logic are unit-testable without AWS.
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from backend.api import connectors_admin
from backend.api.activity import ActivityLog
from backend.api.aml_admin import AmlExportTrigger, AmlScheduleService, ScheduleError
from backend.api.aml_reports import AmlExportAccessError, AmlExportsService
from backend.api.integrations import IntegrationHealthStore, suggest_fix
from backend.api.jobs import DynamoDbJobStore, JobStore
from backend.api.matters import MattersService, QuarterlyTrigger
from backend.api.pipeline import run_resolve, run_search
from backend.api.reports import ReportAccessError, ReportsService
from backend.api.screener import ScreenerAccessError, ScreenerError, ScreenerService
from backend.search import scoping
from backend.search.framework import DEFAULT_ORG_NAME, DEFAULT_ORG_OFFICE_ADDRESS
from backend.search.orchestrator import DossierBuilder
from backend.search.quick_answers import QuickAnswerService
from backend.search.resolve import MatterResolver
from backend.search.synthesize import MatterSynthesizer
from services.logging import get_logger
from services.secrets import SecretsClient

logger = get_logger(__name__)

REGION = os.environ.get("APHRODITE_REGION", "ca-central-1")


def _excluded_folders_from_env() -> frozenset[str]:
    """Parse ``APHRODITE_EXCLUDED_SHAREPOINT_FOLDERS`` (a JSON list, or a
    comma-separated string as a fallback) into excluded folder names.

    A comma signals genuine comma-separated intent; malformed JSON with no
    comma (e.g. a truncated/garbled value) falls back to the default rather
    than being treated as a single-item list of garbage. An explicit empty
    list (``"[]"``) is honored as "exclude nothing" rather than falling back
    to the default; blank/whitespace-only entries are dropped so they can't
    accidentally match every folder.
    """
    raw = os.environ.get("APHRODITE_EXCLUDED_SHAREPOINT_FOLDERS", "").strip()
    if not raw:
        return scoping.DEFAULT_EXCLUDED_FOLDERS
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        parsed = [p.strip() for p in raw.split(",") if p.strip()] if "," in raw else None
    if not isinstance(parsed, list):
        return scoping.DEFAULT_EXCLUDED_FOLDERS
    return frozenset(str(v).strip().lower() for v in parsed if str(v).strip())


@dataclass
class Deps:
    resolver: MatterResolver
    dossier_builder: DossierBuilder
    synthesizer: MatterSynthesizer
    store: JobStore
    self_invoke: Callable[[dict[str, Any]], None]
    reports: ReportsService | None = None
    aml_exports: AmlExportsService | None = None
    matters: MattersService | None = None
    quarterly: QuarterlyTrigger | None = None
    quick: QuickAnswerService | None = None
    screener: ScreenerService | None = None
    aml_trigger: AmlExportTrigger | None = None
    aml_schedule: AmlScheduleService | None = None
    activity: ActivityLog | None = None
    integrations: IntegrationHealthStore | None = None
    secrets: SecretsClient | None = None


_DEPS: Deps | None = None  # cold-start cache / test injection point


def _build_deps(*, model_id: str | None = None) -> Deps:
    """Wire the real connectors + services once per cold start.

    ``model_id`` overrides the Bedrock synthesis model (defaults to the env /
    code default). It exists so an out-of-band measurement harness (the A/B
    runner) can drive the same pipeline against a candidate model without
    changing the deployed default; production callers pass nothing.
    """
    import boto3

    from backend.connectors.clio.connector import ClioConnector
    from backend.connectors.estateably.connector import EstateablyConnector
    from backend.connectors.graph.connector import GraphConnector
    from backend.connectors.quickbooks.connector import QuickBooksConnector
    from services.knowledge_base import KnowledgeBaseClient
    from services.llm import BedrockLLM
    from services.secrets import SecretsClient

    secrets = SecretsClient(boto3.client("secretsmanager", region_name=REGION))
    estateably = EstateablyConnector(secrets)
    clio = ClioConnector(secrets)
    graph = GraphConnector(secrets)
    quickbooks = QuickBooksConnector(secrets)
    # SharePoint documents come from a Bedrock Knowledge Base (full-body index
    # built by the crawler Lambda). Unset ⇒ SharePoint retrieval is skipped.
    kb_id = os.environ.get("APHRODITE_SHAREPOINT_KB_ID")
    knowledge_base = KnowledgeBaseClient(kb_id) if kb_id else None
    resolver = MatterResolver(estateably=estateably, clio=clio)
    # QuickBooks joins as a reconciliation cross-check (matched by name inside the
    # dossier pull), not a resolution source.
    builder = DossierBuilder(
        estateably=estateably,
        clio=clio,
        graph=graph,
        quickbooks=quickbooks,
        knowledge_base=knowledge_base,
        excluded_sharepoint_folders=_excluded_folders_from_env(),
    )
    # Big matters generate a long JSON answer (+ a one-shot retry at up to
    # RETRY_MAX_TOKENS); give Bedrock room to finish well inside the Lambda's
    # 900s timeout (main.tf) rather than tripping boto3's default 60s read
    # timeout mid-generation. Sized for DEFAULT_MAX_TOKENS=22000 at the observed
    # worst-case ~28-33ms/output-token rate (~650-720s) with margin to spare for
    # fan-out/build ahead of it and a possible retry decision after it.
    org_name = os.environ.get("APHRODITE_ORG_NAME", DEFAULT_ORG_NAME)
    office_address = os.environ.get("APHRODITE_ORG_OFFICE_ADDRESS", DEFAULT_ORG_OFFICE_ADDRESS)
    synthesizer = MatterSynthesizer(
        BedrockLLM(model_id=model_id, read_timeout=650),
        org_name=org_name,
        office_address=office_address,
    )
    # Tier-0 quick answers reuse the same connectors (no LLM).
    quick = QuickAnswerService(estateably=estateably, clio=clio, graph=graph, quickbooks=quickbooks)
    store = DynamoDbJobStore(os.environ["APHRODITE_JOBS_TABLE"])

    # Quarterly report drafts are read from the immutable outputs bucket for the
    # portal's Quarterly Reports tab (list + presigned download). Absent ⇒ the
    # /reports routes report "not configured" rather than erroring.
    outputs_bucket = os.environ.get("APHRODITE_OUTPUTS_BUCKET")
    s3 = boto3.client("s3", region_name=REGION) if outputs_bucket else None
    reports = ReportsService(s3, outputs_bucket) if s3 else None
    # AML export drafts (D6) live in the same outputs bucket, under aml/ instead
    # of quarterly/ — no separate bucket or client needed.
    aml_exports = AmlExportsService(s3, outputs_bucket) if s3 else None
    # Sanction Screener: screens uploaded name lists against the watchlist bundle
    # (loaded from the reference bucket, packaged fallback) and writes Certificates
    # of Search to the same outputs bucket under screener/. The matcher is built
    # lazily on first screen, so non-screener requests pay nothing at cold start.
    screener = (
        ScreenerService(
            s3=s3,
            outputs_bucket=outputs_bucket,
            sanctions_bucket=os.environ.get("APHRODITE_SANCTIONS_BUCKET"),
            kms_key_id=os.environ.get("APHRODITE_OUTPUTS_KMS_KEY_ID"),
        )
        if s3 and outputs_bucket
        else None
    )

    fn_name = os.environ["APHRODITE_SELF_FUNCTION"]
    lambda_client = boto3.client("lambda", region_name=REGION)

    def self_invoke(payload: dict[str, Any]) -> None:
        lambda_client.invoke(
            FunctionName=fn_name,
            InvocationType="Event",  # async
            Payload=json.dumps(payload).encode("utf-8"),
        )

    # Temporary on-demand quarterly generation for the portal (until the schedule
    # is on): list open Clio matters + async-invoke the quarterly Lambda by name.
    matters = MattersService(clio)
    quarterly_fn = os.environ.get("APHRODITE_QUARTERLY_FUNCTION")
    quarterly = QuarterlyTrigger(lambda_client, quarterly_fn) if quarterly_fn else None

    # AML Export tab: on-demand "run now" (async-invoke the AML export Lambda) and
    # a self-serve monthly schedule (read/update its EventBridge Scheduler schedule).
    aml_export_fn = os.environ.get("APHRODITE_AML_EXPORT_FUNCTION")
    aml_trigger = AmlExportTrigger(lambda_client, aml_export_fn) if aml_export_fn else None
    aml_schedule_name = os.environ.get("APHRODITE_AML_SCHEDULE_NAME")
    aml_schedule = (
        AmlScheduleService(boto3.client("scheduler", region_name=REGION), aml_schedule_name)
        if aml_schedule_name
        else None
    )

    # Chain-of-custody activity log (Activity Logs tab). Absent ⇒ the /activity
    # route reports "not configured" and the record() calls below are skipped.
    activity_table = os.environ.get("APHRODITE_ACTIVITY_TABLE")
    activity = ActivityLog(activity_table) if activity_table else None

    # Integration health (Integrations tab): the scheduled health_probe writes
    # per-connector status here and GET /integrations reads it. Absent ⇒ the probe
    # is a no-op and the route reports "not configured".
    health_table = os.environ.get("APHRODITE_INTEGRATION_HEALTH_TABLE")
    integrations = IntegrationHealthStore(health_table) if health_table else None

    return Deps(
        resolver,
        builder,
        synthesizer,
        store,
        self_invoke,
        reports,
        aml_exports,
        matters,
        quarterly,
        quick,
        screener,
        aml_trigger,
        aml_schedule,
        activity,
        integrations,
        secrets=secrets,
    )


def _get_deps() -> Deps:
    global _DEPS
    if _DEPS is None:
        _DEPS = _build_deps()
    return _DEPS


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def _actor(event: dict[str, Any]) -> dict[str, Any]:
    """Extract the caller identity from the API GW Cognito JWT authorizer claims."""
    try:
        claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
    except (KeyError, TypeError):
        return {}
    return {"sub": claims.get("sub"), "email": claims.get("email")}


ADMIN_GROUP_NAME = "admins"


def _is_admin(event: dict[str, Any]) -> bool:
    """True if the caller's JWT carries the admin Cognito group.

    API Gateway's JWT authorizer serializes a multi-value claim like
    ``cognito:groups`` as a JSON-array string (e.g. ``'["admins"]'``); a
    single-group membership may come through as a plain string instead. Any
    parse failure is treated as a single group name rather than raising.
    """
    try:
        claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
    except (KeyError, TypeError):
        return False
    raw = claims.get("cognito:groups", "")
    if not raw:
        return False
    if raw.startswith("["):
        try:
            groups = json.loads(raw)
        except (ValueError, TypeError):
            groups = [raw]
    else:
        groups = [raw]
    return ADMIN_GROUP_NAME in groups


def _require_admin(event: dict[str, Any]) -> dict[str, Any] | None:
    """Return a 403 response for a non-admin caller, else ``None``."""
    return None if _is_admin(event) else _response(403, {"error": "admin role required"})


def _body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _scope(body: dict[str, Any]) -> str:
    """Resolution source scope: 'clio' skips Estateably, else 'all' (validated)."""
    return "clio" if body.get("scope") == "clio" else "all"


def _record(deps: Deps, event: dict[str, Any], action: str, summary: str, **fields: Any) -> None:
    """Append a chain-of-custody activity row for the caller, if the log is wired.

    Never raises — :meth:`ActivityLog.record` swallows its own failures."""
    if deps.activity is not None:
        deps.activity.record(_actor(event), action, summary, **fields)


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    # Async worker path (self-invoked) — not an API Gateway request.
    if event.get("worker"):
        return _run_worker(event, context)

    # Scheduled connector health probe (EventBridge Scheduler → this Lambda with
    # {"health_probe": true}) — not an API Gateway request.
    if event.get("health_probe"):
        return _run_health_probe(event, context)

    deps = _get_deps()
    http = event.get("requestContext", {}).get("http", {})
    method, path = http.get("method", ""), http.get("path", "")

    if method == "POST" and path.endswith("/resolve"):
        body = _body(event)
        query = body.get("query", "").strip()
        if not query:
            return _response(400, {"error": "missing 'query'"})
        return _response(200, run_resolve(query, resolver=deps.resolver, scope=_scope(body)))

    if method == "POST" and path.endswith("/search"):
        body = _body(event)
        query = body.get("query", "").strip()
        if not query:
            return _response(400, {"error": "missing 'query'"})
        # Optional: a candidate the UI already picked from a prior disambiguation,
        # so the worker resolves to that matter directly instead of re-matching.
        selection = body.get("selection") if isinstance(body.get("selection"), dict) else None
        # Optional: force the full status-file synthesis (skip Tier-0 quick answers),
        # e.g. the "view full status file" escalation from a quick answer.
        full = bool(body.get("full"))
        # Optional: "clio" skips Estateably in resolution so an old Clio + SharePoint
        # matter (no Estateably record) surfaces instead of being outranked.
        scope = _scope(body)
        job_id = uuid.uuid4().hex
        actor = _actor(event)
        deps.store.create(job_id, query, actor)
        deps.self_invoke(
            {
                "worker": True,
                "job_id": job_id,
                "query": query,
                "actor": actor,
                "selection": selection,
                "full": full,
                "scope": scope,
            }
        )
        return _response(202, {"job_id": job_id, "status": "pending"})

    # Per-user search history (background/history/bell). Keyed by the caller's
    # Cognito sub from the JWT — never a client-supplied value.
    if method == "GET" and path.endswith("/search"):
        sub = _actor(event).get("sub")
        searches = deps.store.list_for_actor(sub) if sub else []
        return _response(200, {"searches": searches})

    # Connector-health banner: source errors from the caller's most recent full
    # search (e.g. a dead QuickBooks token) — surfaced in-app so degradation isn't
    # silent. No email/SNS; the SPA polls this and shows a dismissible banner.
    if method == "GET" and path.endswith("/connector-health"):
        sub = _actor(event).get("sub")
        errors = deps.store.latest_source_errors(sub) if sub else []
        return _response(200, {"source_errors": errors})

    if method == "GET" and "/search/" in path:
        job_id = (event.get("pathParameters") or {}).get("jobId", "")
        job = deps.store.get(job_id)
        if job is None:
            return _response(404, {"error": "job not found"})
        # Per-user isolation: only the owner may read a job. Return 404 (not 403)
        # so a job's existence isn't disclosed to other users.
        owner = job.get("actor_sub")
        if owner and owner != _actor(event).get("sub"):
            return _response(404, {"error": "job not found"})
        return _response(200, job)

    # Staff can clear a stale/errored search from their own history list. The
    # chain-of-custody audit trail (search.completed, emitted when the search
    # itself ran) is untouched — this only removes the convenience list entry.
    if method == "DELETE" and "/search/" in path:
        job_id = (event.get("pathParameters") or {}).get("jobId", "")
        job = deps.store.get(job_id)
        if job is None:
            return _response(404, {"error": "job not found"})
        owner = job.get("actor_sub")
        if owner and owner != _actor(event).get("sub"):
            return _response(404, {"error": "job not found"})
        deps.store.delete(job_id)
        return _response(200, {"deleted": job_id})

    if method == "GET" and path.endswith("/reports/download"):
        if deps.reports is None:
            return _response(503, {"error": "reports not configured"})
        key = (event.get("queryStringParameters") or {}).get("key", "")
        try:
            url = deps.reports.download_url(key)
        except ReportAccessError:
            return _response(400, {"error": "invalid report key"})
        _record(deps, event, "download", f"Downloaded quarterly report {key}", target=key)
        return _response(200, {"url": url})

    if method == "GET" and path.endswith("/reports"):
        if deps.reports is None:
            return _response(503, {"error": "reports not configured"})
        return _response(200, {"reports": deps.reports.list()})

    if method == "GET" and path.endswith("/aml-exports/download"):
        if deps.aml_exports is None:
            return _response(503, {"error": "aml exports not configured"})
        key = (event.get("queryStringParameters") or {}).get("key", "")
        try:
            url = deps.aml_exports.download_url(key)
        except AmlExportAccessError:
            return _response(400, {"error": "invalid export key"})
        _record(deps, event, "download", f"Downloaded AML export workbook {key}", target=key)
        return _response(200, {"url": url})

    if method == "GET" and path.endswith("/aml-exports"):
        if deps.aml_exports is None:
            return _response(503, {"error": "aml exports not configured"})
        return _response(200, {"exports": deps.aml_exports.list()})

    # On-demand "run now" for the AML export (async-invoke; the SPA polls the list).
    if method == "POST" and path.endswith("/aml-exports/generate"):
        if deps.aml_trigger is None:
            return _response(503, {"error": "aml generation not configured"})
        deps.aml_trigger.run()
        _record(deps, event, "aml.run", "Ran the AML export on demand")
        return _response(202, {"status": "generating"})

    # Self-serve monthly schedule for the AML export (read + update the schedule).
    if method == "GET" and path.endswith("/aml-exports/schedule"):
        if deps.aml_schedule is None:
            return _response(503, {"error": "aml schedule not configured"})
        return _response(200, deps.aml_schedule.get())

    if method == "PUT" and path.endswith("/aml-exports/schedule"):
        if deps.aml_schedule is None:
            return _response(503, {"error": "aml schedule not configured"})
        body = _body(event)
        try:
            state = deps.aml_schedule.update(
                enabled=bool(body.get("enabled")),
                day_of_month=body.get("day_of_month"),
                hour=body.get("hour"),
                minute=body.get("minute"),
            )
        except ScheduleError as exc:
            return _response(400, {"error": str(exc)})
        if state.get("enabled"):
            when = (
                f"day {state.get('day_of_month')} at "
                f"{state.get('hour', 0):02d}:{state.get('minute', 0):02d}"
            )
            summary = f"Set the AML export schedule to run monthly on {when}"
        else:
            summary = "Turned off the automatic AML export schedule"
        _record(deps, event, "aml.schedule", summary, detail=state)
        return _response(200, state)

    if method == "GET" and path.endswith("/matters"):
        if deps.matters is None:
            return _response(503, {"error": "matters not configured"})
        return _response(200, {"matters": deps.matters.list_open()})

    if method == "POST" and path.endswith("/reports/generate"):
        if deps.quarterly is None:
            return _response(503, {"error": "generation not configured"})
        body = _body(event)
        matter_id = str(body.get("matter_id", "")).strip()
        try:
            year, quarter = int(body["year"]), int(body["quarter"])
        except (KeyError, TypeError, ValueError):
            return _response(400, {"error": "missing/invalid 'matter_id', 'year', or 'quarter'"})
        if not matter_id or quarter not in (1, 2, 3, 4):
            return _response(400, {"error": "missing/invalid 'matter_id', 'year', or 'quarter'"})
        deps.quarterly.generate(matter_id, year, quarter)
        _record(
            deps,
            event,
            "report.generate",
            f"Generated a Q{quarter} {year} quarterly report draft",
            target=matter_id,
            detail={"matter_id": matter_id, "year": year, "quarter": quarter},
        )
        return _response(202, {"status": "generating", "matter_id": matter_id})

    # -- Sanction Screener (D6 companion) -----------------------------------
    if method == "POST" and path.endswith("/screener/screen"):
        if deps.screener is None:
            return _response(503, {"error": "screener not configured"})
        raw = event.get("body") or ""
        content = base64.b64decode(raw) if event.get("isBase64Encoded") else raw.encode("utf-8")
        headers = event.get("headers") or {}
        filename = headers.get("x-file-name") or headers.get("X-File-Name") or ""
        try:
            result = deps.screener.screen(content, filename)
        except ScreenerError as exc:
            return _response(400, {"error": str(exc)})
        screened = result.get("total_screened", 0)
        matches = result.get("total_matches", 0)
        _record(
            deps,
            event,
            "screener.screen",
            f"Screened {screened} name(s) against the sanctions watchlists "
            f"({matches} potential match(es))",
            target=filename or None,
            detail={"total_screened": screened, "total_matches": matches},
        )
        return _response(200, result)

    if method == "POST" and path.endswith("/screener/certificate"):
        if deps.screener is None:
            return _response(503, {"error": "screener not configured"})
        try:
            cert = deps.screener.certificate(_body(event), _actor(event))
        except ScreenerError as exc:
            return _response(400, {"error": str(exc)})
        _record(
            deps,
            event,
            "screener.certificate",
            "Issued a Certificate of Search",
            target=cert.get("key"),
        )
        return _response(200, cert)

    if method == "GET" and path.endswith("/screener/certificates/download"):
        if deps.screener is None:
            return _response(503, {"error": "screener not configured"})
        key = (event.get("queryStringParameters") or {}).get("key", "")
        try:
            url = deps.screener.download_url(key)
        except ScreenerAccessError:
            return _response(400, {"error": "invalid certificate key"})
        _record(deps, event, "download", "Downloaded a Certificate of Search", target=key)
        return _response(200, {"url": url})

    if method == "GET" and path.endswith("/screener/certificates"):
        if deps.screener is None:
            return _response(503, {"error": "screener not configured"})
        return _response(200, {"certificates": deps.screener.list()})

    if method == "DELETE" and path.endswith("/screener/certificates"):
        if deps.screener is None:
            return _response(503, {"error": "screener not configured"})
        key = (event.get("queryStringParameters") or {}).get("key", "")
        try:
            removed = deps.screener.delete(key)
        except ScreenerAccessError:
            return _response(400, {"error": "invalid certificate key"})
        _record(deps, event, "screener.delete", "Removed a Certificate of Search", target=key)
        return _response(200, {"deleted": key, "removed": removed})

    # -- Activity Logs (chain-of-custody audit trail) -----------------------
    if method == "GET" and path.endswith("/activity"):
        if deps.activity is None:
            return _response(503, {"error": "activity log not configured"})
        limit = int((event.get("queryStringParameters") or {}).get("limit") or 500)
        return _response(200, {"activity": deps.activity.list(limit)})

    # -- Integrations health (live connector status + downtime log) ---------
    if method == "GET" and path.endswith("/integrations"):
        if deps.integrations is None:
            return _response(503, {"error": "integration health not configured"})
        return _response(200, deps.integrations.snapshot())

    # -- Connector onboarding / reconnect (admin-gated credential writes) ---
    if method == "POST" and path.endswith("/connectors/estateably"):
        forbidden = _require_admin(event)
        if forbidden:
            return forbidden
        try:
            result = connectors_admin.connect_estateably(deps.secrets, _body(event))
        except connectors_admin.ConnectorValidationError as exc:
            return _response(400, {"error": str(exc)})
        return _response(200, result)

    if method == "POST" and path.endswith("/connectors/graph"):
        forbidden = _require_admin(event)
        if forbidden:
            return forbidden
        try:
            result = connectors_admin.connect_graph(deps.secrets, _body(event))
        except connectors_admin.ConnectorValidationError as exc:
            return _response(400, {"error": str(exc)})
        return _response(200, result)

    return _response(404, {"error": "not found"})


def _run_health_probe(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Probe every connector (read-only) and record status to the health store.

    Invoked on a fixed schedule (EventBridge Scheduler → this Lambda), not an API
    request. Reuses the D2 connectivity harness; each source's error detail is
    mapped to a plain-language suggested fix for the Integrations tab. Never raises
    into the scheduler."""
    deps = _get_deps()
    if deps.integrations is None:
        logger.warning("health probe skipped: integration health table not configured")
        return {"ok": False, "reason": "not configured"}

    import boto3

    from backend.harness import run_checks
    from services.secrets import SecretsClient

    secrets = SecretsClient(boto3.client("secretsmanager", region_name=REGION))
    report = run_checks(secrets=secrets)
    for name, result in report.get("sources", {}).items():
        ok = bool(result.get("ok"))
        detail = str(result.get("detail", ""))
        fix = "" if ok else suggest_fix(name, detail)
        deps.integrations.record_result(name, ok=ok, detail=detail, suggested_fix=fix)
    logger.info("health probe complete", extra={"extra_fields": {"ok": report.get("ok")}})
    return {"ok": report.get("ok"), "checked": sorted(report.get("sources", {}).keys())}


def _run_worker(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Process one search job: run the pipeline and store the result."""
    deps = _get_deps()
    job_id, query = event["job_id"], event["query"]
    # The Lambda context exposes the remaining execution budget; the synthesizer
    # uses it to skip its truncation retry when there isn't time for another full
    # generation (so a second call can't run past the timeout and strand the job).
    remaining_ms = context.get_remaining_time_in_millis if context is not None else None

    def on_phase(phase: str) -> None:
        # Live progress for the poller; a write failure must never break the search.
        try:
            deps.store.progress(job_id, phase)
        except Exception:  # noqa: BLE001
            logger.warning("progress write failed", extra={"extra_fields": {"job_id": job_id}})

    try:
        result = run_search(
            query,
            resolver=deps.resolver,
            dossier_builder=deps.dossier_builder,
            synthesizer=deps.synthesizer,
            actor=event.get("actor"),
            selection=event.get("selection"),
            quick=deps.quick,
            full=bool(event.get("full")),
            scope=event.get("scope") or "all",
            on_phase=on_phase,
            activity=deps.activity,
            remaining_ms=remaining_ms,
        )
        deps.store.complete(job_id, result)
        return {"ok": True, "job_id": job_id}
    except Exception as exc:  # noqa: BLE001 - record failure on the job, don't crash the worker
        logger.exception("search worker failed", extra={"extra_fields": {"job_id": job_id}})
        deps.store.fail(job_id, str(exc))
        return {"ok": False, "job_id": job_id}
