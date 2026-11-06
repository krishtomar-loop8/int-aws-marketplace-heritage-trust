"""EventBridge entry point for the monthly AML name export (D6).

Scheduled for 00:00 on the 1st of each month. Pulls the active Estateably
roster, builds the Estates/Trusts/POA workbook, and writes it plus a
manifest to the immutable S3 outputs bucket for a human to review and
screen in the in-app Sanction Screener.

This job never calls Bedrock and never writes back to a source system —
see ``export.py`` and the module README for the full constraint list.
SharePoint upload is not yet implemented (README Open Item #5); the S3
object is the source of truth for the POC.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from backend.lambdas.aml_export import quality
from backend.lambdas.aml_export.designations import load_designations
from backend.lambdas.aml_export.excel import build_workbook_bytes
from backend.lambdas.aml_export.export import run_export
from backend.lambdas.aml_export.storage import AmlExportStorage
from services.logging import get_logger

logger = get_logger(__name__)

REGION = os.environ.get("APHRODITE_REGION", "ca-central-1")


def _json_list_from_env(var_name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """Parse a JSON-list env var (comma-separated string as a fallback) into a
    tuple, falling back to ``default`` on an unset or malformed value. A comma
    signals genuine comma-separated intent; malformed JSON with no comma falls
    back to the default rather than being treated as a single-item list. An
    explicit empty list (``"[]"``) is honored as-is rather than falling back
    to the default; blank/whitespace-only entries are dropped so they can't
    accidentally match everything (an empty own-company name, title keyword,
    or folder name would otherwise match any string)."""
    raw = os.environ.get(var_name, "").strip()
    if not raw:
        return default
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        parsed = [p.strip() for p in raw.split(",") if p.strip()] if "," in raw else None
    if not isinstance(parsed, list):
        return default
    return tuple(str(v).strip() for v in parsed if str(v).strip())


def _org_config_from_env() -> quality.OrgConfig:
    default = quality.OrgConfig()
    return quality.OrgConfig(
        own_company_names=_json_list_from_env(
            "APHRODITE_OWN_COMPANY_NAMES", default.own_company_names
        ),
        internal_title_keywords=_json_list_from_env(
            "APHRODITE_INTERNAL_TITLE_KEYWORDS", default.internal_title_keywords
        ),
        internal_number_pattern=os.environ.get(
            "APHRODITE_INTERNAL_MATTER_NUMBER_PATTERN", default.internal_number_pattern
        ),
    )


@dataclass
class Deps:
    estateably: Any
    clio: Any
    storage: AmlExportStorage
    notify: Callable[[dict[str, str], dict[str, Any]], None]
    designations_bucket: str | None = None
    designations_key: str | None = None


_DEPS: Deps | None = None  # cold-start cache / test injection point


def _build_deps() -> Deps:
    quality.configure(_org_config_from_env())

    import boto3

    from backend.connectors.clio.connector import ClioConnector
    from backend.connectors.estateably.connector import EstateablyConnector
    from services.secrets import SecretsClient

    secrets = SecretsClient(boto3.client("secretsmanager", region_name=REGION))
    estateably = EstateablyConnector(secrets)
    clio = ClioConnector(secrets)
    storage = AmlExportStorage(
        s3=boto3.client("s3", region_name=REGION),
        bucket=os.environ["APHRODITE_OUTPUTS_BUCKET"],
        prefix=os.environ.get("APHRODITE_AML_EXPORT_PREFIX", "aml-exports"),
        kms_key_id=os.environ.get("APHRODITE_OUTPUTS_KMS_KEY_ID"),
    )

    topic_arn = os.environ.get("APHRODITE_NOTIFY_TOPIC_ARN")
    sns = boto3.client("sns", region_name=REGION) if topic_arn else None

    def notify(keys: dict[str, str], result: dict[str, Any]) -> None:
        if not sns:
            return
        summary = result.get("summary", {})
        sns.publish(
            TopicArn=topic_arn,
            Subject="AML name export ready for review",
            Message=(
                "The monthly AML name export is ready for review and upload to the "
                f"Sanctions Screener.\nS3 key: {keys['workbook']}\n"
                f"Parties: {len(result['parties'])}, excluded matters: {len(result['excluded'])}, "
                f"exceptions: {len(result['exceptions'])}, records needing review: "
                f"{summary.get('records_needing_review', 0)}."
            ),
        )

    return Deps(
        estateably=estateably,
        clio=clio,
        storage=storage,
        notify=notify,
        designations_bucket=os.environ.get("APHRODITE_REFERENCE_BUCKET"),
        designations_key=os.environ.get("APHRODITE_AML_DESIGNATIONS_KEY"),
    )


def _get_deps() -> Deps:
    global _DEPS
    if _DEPS is None:
        _DEPS = _build_deps()
    return _DEPS


def handler(event: dict[str, Any] | None = None, context: Any = None) -> dict[str, Any]:
    del event, context
    deps = _get_deps()

    now = datetime.now(UTC)
    designations = load_designations(
        deps.storage.s3, deps.designations_bucket, deps.designations_key
    )
    prior_matter_numbers = deps.storage.load_prior_matter_numbers(now)

    result = run_export(
        deps.estateably,
        deps.clio,
        designations=designations,
        prior_matter_numbers=prior_matter_numbers,
    )
    workbook_bytes = build_workbook_bytes(result)

    keys = deps.storage.put_export(workbook_bytes, result, now=now)

    logger.info(
        "AML export written to S3",
        extra={
            "extra_fields": {
                "bucket": deps.storage.bucket,
                "key": keys["workbook"],
                "parties": len(result["parties"]),
                "excluded": len(result["excluded"]),
                "exceptions": len(result["exceptions"]),
            }
        },
    )
    deps.notify(keys, result)
    return {"bucket": deps.storage.bucket, "key": keys["workbook"], "manifest": keys["manifest"]}
