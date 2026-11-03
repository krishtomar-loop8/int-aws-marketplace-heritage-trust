"""EventBridge entry point for the monthly sanctions-data refresh.

Fetches the three public government watchlists (UN Consolidated, Canada SEMA,
Public Safety Canada) over HTTPS, and writes a fresh bundle to the reference
S3 bucket that the Sanction Screener loads at cold start. This keeps the
screening data current without a code deploy — the source tool shipped a
static January snapshot, which goes stale.

Only public sanctions data is fetched (no client PII leaves the platform), and
the bundle is written at rest in ca-central-1. If a source fetch fails and any
list comes back empty, the run aborts rather than overwrite a good bundle with
a partial one.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from services.logging import get_logger

logger = get_logger(__name__)

REGION = os.environ.get("APHRODITE_REGION", "ca-central-1")
_JSON_MIME = "application/json"


@dataclass
class Deps:
    s3: Any
    bucket: str
    key: str
    kms_key_id: str | None


_DEPS: Deps | None = None  # cold-start cache / test injection point


def _build_deps() -> Deps:
    import boto3

    return Deps(
        s3=boto3.client("s3", region_name=REGION),
        bucket=os.environ["APHRODITE_SANCTIONS_BUCKET"],
        key=os.environ.get("APHRODITE_SANCTIONS_KEY", "sanctions/bundle.json"),
        kms_key_id=os.environ.get("APHRODITE_SANCTIONS_KMS_KEY_ID"),
    )


def _get_deps() -> Deps:
    global _DEPS
    if _DEPS is None:
        _DEPS = _build_deps()
    return _DEPS


def handler(event: dict[str, Any] | None = None, context: Any = None) -> dict[str, Any]:
    del event, context
    deps = _get_deps()

    from backend.screener import fetcher

    data = fetcher.fetch_all(force_http=True)
    un, sema, ps = data["un_list"], data["sema_list"], data["ps_list"]
    counts = {
        "un": len(un),
        "sema": len(sema),
        "ps": len(ps),
        "total": len(un) + len(sema) + len(ps),
    }

    # A partial fetch (some source down) must not clobber a good bundle.
    if not (un and sema and ps):
        raise RuntimeError(f"incomplete sanctions fetch, refusing to overwrite bundle: {counts}")

    bundle = {
        "generated_at": datetime.now(UTC).isoformat(),
        "counts": counts,
        "un_list": un,
        "sema_list": sema,
        "ps_list": ps,
    }
    body = json.dumps(bundle).encode()

    extra: dict[str, Any] = {"ContentType": _JSON_MIME}
    if deps.kms_key_id:
        extra["ServerSideEncryption"] = "aws:kms"
        extra["SSEKMSKeyId"] = deps.kms_key_id
    deps.s3.put_object(Bucket=deps.bucket, Key=deps.key, Body=body, **extra)

    logger.info(
        "sanctions bundle refreshed",
        extra={"extra_fields": {"bucket": deps.bucket, "key": deps.key, **counts}},
    )
    return {"bucket": deps.bucket, "key": deps.key, "counts": counts}
