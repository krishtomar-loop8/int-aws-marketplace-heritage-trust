"""Lambda entry point for the Quarterly Report Draft Generator (D5).

Invoked two ways:

- **EventBridge Scheduler** (quarterly) with an empty/minimal event → reports on
  the previous complete calendar quarter for the configured matter set.
- **Manual invoke** with ``{"matter_ids": [...], "period": {"year": Y,
  "quarter": Q}, "letter_date": "YYYY-MM-DD"}`` to (re)generate a specific set —
  used for the live probe and ad-hoc re-runs after a data correction.

For each matter it builds the :class:`ReportModel` from Clio, renders the cover
letter (.docx) + trust ledger (.pdf), and writes both plus a manifest to the
immutable S3 outputs bucket. Per-matter failures are recorded and skipped so one
bad matter never aborts the batch. Nothing is sent to clients — the drafts land
in S3 for human review (surfaced in the portal's Quarterly Reports tab).

On **scheduled** runs (the EventBridge target sets ``{"sharepoint": true}``) the
drafts are also uploaded to Nicole's SharePoint review folder in a canonical
per-quarter layout (see :mod:`delivery`); the on-demand portal path stays S3-only.
Matters are generated sequentially with a configurable inter-matter delay to pace
Clio's ~50 req/min limit.

**Matter selection:** an explicit ``matter_ids`` in the event always wins (the
on-demand portal path). Otherwise, when the event sets ``{"discover": true}``
(the scheduled EventBridge target), the matter set is discovered from Clio by
practice area, minus any matter with the "Report Exclusion" checkbox ticked (see
:mod:`selection`, Nicole's 2026-08-06 decision). Failing both, it falls back to the
``APHRODITE_QUARTERLY_MATTER_IDS`` env list (empty ⇒ no-op). A ``{"discover_only": true}``
(alias ``dry_run``) event lists what *would* be selected — plus the matters
flagged for having no practice area — without generating anything; it backs the
validation probe. See the module README.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from services.logging import get_logger

from .classification import load_classification
from .delivery import SharePointDelivery
from .generator import ReportGenerator
from .period import ReportingPeriod, previous_quarter
from .render_docx import render_cover_letter
from .render_pdf import render_trust_ledger
from .selection import MatterRow, Selection, select_matters
from .storage import ReportStorage

logger = get_logger(__name__)

REGION = os.environ.get("APHRODITE_REGION", "ca-central-1")


@dataclass
class Deps:
    generator: ReportGenerator
    storage: ReportStorage
    notify: Callable[..., None]
    # Optional SharePoint delivery (scheduled runs only) + inter-matter pacing.
    delivery: SharePointDelivery | None = None
    delay_seconds: float = 0.0
    sleep: Callable[[float], None] = time.sleep
    # Practice-area discovery (scheduled runs): the Clio connector + the rule config.
    clio: Any = None
    qualifying_practice_areas: tuple[str, ...] = ()
    exclusion_field: str = ""


_DEPS: Deps | None = None  # cold-start cache / test injection point


def _build_deps() -> Deps:
    import boto3

    from backend.connectors.clio.connector import ClioConnector
    from services.secrets import SecretsClient

    secrets = SecretsClient(boto3.client("secretsmanager", region_name=REGION))
    s3 = boto3.client("s3", region_name=REGION)
    # Staff-editable account-name → asset-category map from the reference bucket;
    # absent ⇒ in-code defaults (Nicole's Decision A rules). See classification.py.
    classifier = load_classification(
        s3,
        os.environ.get("APHRODITE_REFERENCE_BUCKET"),
        os.environ.get("APHRODITE_ACCOUNT_CLASSIFICATION_KEY"),
    )
    clio = ClioConnector(secrets)
    generator = ReportGenerator(clio, classifier, estateably=_build_estateably(secrets))
    storage = ReportStorage(
        s3=s3,
        bucket=os.environ["APHRODITE_OUTPUTS_BUCKET"],
        kms_key_id=os.environ.get("APHRODITE_OUTPUTS_KMS_KEY_ID"),
    )

    topic_arn = os.environ.get("APHRODITE_NOTIFY_TOPIC_ARN")
    sns = boto3.client("sns", region_name=REGION) if topic_arn else None

    def notify(
        period: ReportingPeriod,
        results: list[dict[str, Any]],
        flagged: list[MatterRow] | None = None,
    ) -> None:
        if not sns:
            return
        ok = [r for r in results if r.get("ok")]
        failed = [r for r in results if not r.get("ok")]
        sp_ok = [r for r in results if r.get("sharepoint") == "ok"]
        sp_failed = [r for r in results if r.get("sharepoint") == "failed"]
        sp_line = ""
        if sp_ok or sp_failed:
            sp_line = f"\nSharePoint: {len(sp_ok)} uploaded, {len(sp_failed)} failed."
        flagged_line = ""
        if flagged:
            flagged_line = (
                f"\n\nAttention: {len(flagged)} open matter(s) have no practice area — please set "
                'one (or tick "Report Exclusion" to omit them) before the next run:\n'
                + "\n".join(f"- {r.number or r.id}: {r.client}" for r in flagged)
            )
        sns.publish(
            TopicArn=topic_arn,
            Subject=f"Quarterly report drafts ready — {period.code}",
            Message=(
                f"{len(ok)} quarterly trust statement draft(s) for {period.code} are ready "
                f"for review in the portal.\nFailed: {len(failed)}.{sp_line}\n\n"
                + "\n".join(
                    f"- {r.get('matter')}: {'ok' if r.get('ok') else r.get('error')}"
                    for r in results
                )
                + flagged_line
            ),
        )

    return Deps(
        generator,
        storage,
        notify,
        delivery=_build_delivery(secrets),
        delay_seconds=float(os.environ.get("APHRODITE_QUARTERLY_MATTER_DELAY_SECONDS", "1.2")),
        clio=clio,
        qualifying_practice_areas=_qualifying_from_env(),
        exclusion_field=os.environ.get("APHRODITE_QUARTERLY_EXCLUSION_FIELD", "").strip(),
    )


def _qualifying_from_env() -> tuple[str, ...]:
    """Parse ``APHRODITE_QUARTERLY_QUALIFYING_PRACTICE_AREAS`` (a JSON list, or a
    comma-separated string as a fallback) into practice-area names."""
    raw = os.environ.get("APHRODITE_QUARTERLY_QUALIFYING_PRACTICE_AREAS", "").strip()
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return tuple(p.strip() for p in raw.split(",") if p.strip())
    if isinstance(parsed, list):
        return tuple(str(v) for v in parsed)
    return ()


def _build_estateably(secrets: Any) -> Any:
    """The Estateably connector for beneficiary addressing, or None to address the
    Clio client (legacy behavior). Gated by ``APHRODITE_QUARTERLY_BENEFICIARY_EMAILS``
    (default on). Construction is lazy (no network), and the generator degrades to
    the Clio client per matter if Estateably is unreachable — so this never blocks
    generation.
    """
    if os.environ.get("APHRODITE_QUARTERLY_BENEFICIARY_EMAILS", "true").strip().lower() not in (
        "1",
        "true",
        "yes",
    ):
        return None
    try:
        from backend.connectors.estateably.connector import EstateablyConnector

        return EstateablyConnector(secrets)
    except Exception:  # noqa: BLE001 - fall back to Clio-client addressing
        logger.exception("estateably setup failed; addressing the Clio client instead")
        return None


def _build_delivery(secrets: Any) -> SharePointDelivery | None:
    """Set up SharePoint delivery from env, or None (S3-only) when off/misconfigured.

    A setup failure (bad site/folder, missing write grant) must never break S3
    generation, so it degrades to S3-only with a logged warning.
    """
    if os.environ.get("APHRODITE_SP_UPLOAD_ENABLED", "").strip().lower() not in ("1", "true", "yes"):
        return None
    try:
        from backend.connectors.graph.publisher import SharePointPublisher

        publisher = SharePointPublisher(secrets)
        site_url = os.environ["APHRODITE_SP_UPLOAD_SITE"]
        folder_path = os.environ["APHRODITE_SP_UPLOAD_FOLDER"]
        site_id = publisher.resolve_site_id(site_url)
        root = publisher.get_item_by_path(site_id, folder_path)
        logger.info(
            "sharepoint delivery enabled",
            extra={"extra_fields": {"site": site_url, "folder": folder_path}},
        )
        return SharePointDelivery(publisher=publisher, site_id=site_id, root_item_id=root["id"])
    except Exception:  # noqa: BLE001 - never let a SharePoint misconfig break S3 generation
        logger.exception("sharepoint delivery setup failed; continuing S3-only")
        return None


def _get_deps() -> Deps:
    global _DEPS
    if _DEPS is None:
        _DEPS = _build_deps()
    return _DEPS


def _resolve_period(event: dict[str, Any], *, today: date) -> ReportingPeriod:
    spec = event.get("period") or {}
    if spec.get("year") and spec.get("quarter"):
        return ReportingPeriod(int(spec["year"]), int(spec["quarter"]))
    return previous_quarter(today)


def _resolve_matter_ids(event: dict[str, Any]) -> list[str]:
    ids = event.get("matter_ids")
    if not ids:
        ids = json.loads(os.environ.get("APHRODITE_QUARTERLY_MATTER_IDS", "[]"))
    return [str(i) for i in ids]


def _resolve_letter_date(event: dict[str, Any], *, today: date) -> date:
    raw = event.get("letter_date")
    if raw:
        return date.fromisoformat(str(raw)[:10])
    return today


def handler(event: dict[str, Any] | None = None, context: Any = None) -> dict[str, Any]:
    event = event or {}
    deps = _get_deps()
    now = datetime.now(UTC)
    today = now.date()

    period = _resolve_period(event, today=today)
    letter_date = _resolve_letter_date(event, today=today)
    generated_at = now.isoformat()

    # Selection: explicit ids (portal) win; else discover by practice area on
    # scheduled runs; else the env fallback list. discover_only lists the set
    # without generating (validation probe).
    explicit_ids = event.get("matter_ids")
    discover_only = bool(event.get("discover_only") or event.get("dry_run"))
    discover = discover_only or (bool(event.get("discover")) and not explicit_ids)
    selection = _discover(deps) if discover else None

    if discover_only and selection is not None:
        logger.info(
            "discover-only run",
            extra={
                "extra_fields": {
                    "selected": len(selection.selected),
                    "flagged_no_practice_area": len(selection.flagged_no_area),
                    "excluded": len(selection.excluded),
                }
            },
        )
        return {"period": period.code, "mode": "discover_only", **selection.as_dict()}

    matter_ids = selection.selected_ids if selection is not None else _resolve_matter_ids(event)

    if not matter_ids:
        logger.warning("no matter_ids configured; nothing to generate")
        result: dict[str, Any] = {
            "period": period.code,
            "generated": [],
            "failed": [],
            "skipped": "no_matters",
        }
        if selection is not None:
            result["flagged_no_practice_area"] = [r.as_dict() for r in selection.flagged_no_area]
        return result

    # SharePoint delivery is opt-in per invocation (scheduled runs set it), and only
    # when a publisher was configured. On-demand portal generation stays S3-only.
    deliver = deps.delivery is not None and bool(event.get("sharepoint"))

    logger.info(
        "generating quarterly drafts",
        extra={
            "extra_fields": {
                "period": period.code,
                "matter_count": len(matter_ids),
                "sharepoint": deliver,
            }
        },
    )

    results: list[dict[str, Any]] = []
    for index, matter_id in enumerate(matter_ids):
        if index and deps.delay_seconds:
            deps.sleep(deps.delay_seconds)  # pace the source APIs (Clio ~50 req/min)
        results.append(
            _generate_one(deps, matter_id, period, letter_date, generated_at, deliver=deliver)
        )

    flagged = selection.flagged_no_area if selection is not None else None
    deps.notify(period, results, flagged=flagged)
    out: dict[str, Any] = {
        "period": period.code,
        "generated": [r["matter"] for r in results if r["ok"]],
        "failed": [{"matter": r["matter"], "error": r["error"]} for r in results if not r["ok"]],
        "sharepoint_uploaded": [r["matter"] for r in results if r.get("sharepoint") == "ok"],
    }
    if selection is not None:
        out["flagged_no_practice_area"] = [r.as_dict() for r in selection.flagged_no_area]
    return out


def _discover(deps: Deps) -> Selection:
    """Discover the scheduled matter set from Clio by practice area (minus exclusions)."""
    return select_matters(
        deps.clio,
        qualifying=deps.qualifying_practice_areas,
        exclusion_field=deps.exclusion_field,
    )


def _generate_one(
    deps: Deps,
    matter_id: str,
    period: ReportingPeriod,
    letter_date: date,
    generated_at: str,
    *,
    deliver: bool = False,
) -> dict[str, Any]:
    try:
        model = deps.generator.build(matter_id, period, letter_date=letter_date)
        docx = render_cover_letter(model)
        pdf = render_trust_ledger(model)
        keys = deps.storage.put_report(model, docx, pdf, generated_at=generated_at)
        logger.info(
            "generated draft",
            extra={
                "extra_fields": {
                    "matter": model.matter_number,
                    "prefix": deps.storage.prefix(model),
                }
            },
        )
        result: dict[str, Any] = {"matter": model.matter_number, "ok": True, "keys": keys}
        if deliver and deps.delivery is not None:
            # Best-effort: a SharePoint failure must not discard the saved S3 draft.
            try:
                result["sharepoint_url"] = deps.delivery.deliver(model, docx, pdf)
                result["sharepoint"] = "ok"
            except Exception:  # noqa: BLE001 - S3 draft already saved; SharePoint is a copy
                logger.warning(
                    "sharepoint upload failed",
                    extra={"extra_fields": {"matter": model.matter_number}},
                )
                result["sharepoint"] = "failed"
        return result
    except Exception as exc:  # noqa: BLE001 - isolate one matter's failure from the batch
        logger.exception(
            "failed to generate draft", extra={"extra_fields": {"matter": str(matter_id)}}
        )
        return {"matter": str(matter_id), "ok": False, "error": str(exc)}
