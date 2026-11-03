"""Lambda entry point for the scheduled SharePoint crawler.

Env:
- ``APHRODITE_REGION``                    AWS region for Graph secret + S3 (default ca-central-1)
- ``APHRODITE_SHAREPOINT_SITES``          JSON list of granted SharePoint site URLs
- ``APHRODITE_CRAWL_BUCKET``              S3 bucket for crawled documents
- ``APHRODITE_CRAWL_KMS_KEY_ID``          (optional) CMK for SSE-KMS on crawled objects
- ``APHRODITE_SELF_FUNCTION``             (optional) this function's name, for self-continue
- ``APHRODITE_SHAREPOINT_KB_ID``          (optional) Knowledge Base to re-ingest after crawl
- ``APHRODITE_SHAREPOINT_DATA_SOURCE_ID`` (optional) KB data source id to ingest
- ``APHRODITE_KB_REGION``                 (optional) region the KB lives in (default APHRODITE_REGION)

Large libraries can't finish in one 900s invocation, so the crawler checkpoints
per page and stops before the deadline; this handler then **self-invokes** to
continue from the saved cursor until the backfill drains. Ingestion is triggered
only once the crawl actually completes.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from backend.lambdas.sharepoint_crawler.crawl import SharePointCrawler
from backend.lambdas.sharepoint_crawler.storage import S3CrawlStorage
from services.logging import get_logger

logger = get_logger(__name__)

REGION = os.environ.get("APHRODITE_REGION", "ca-central-1")
# Stop this many seconds before the Lambda deadline to checkpoint + self-invoke.
_DEADLINE_SAFETY_SECONDS = 90.0
# Backstop against a runaway self-invoke chain.
_MAX_CONTINUES = 200


def handler(event: dict[str, Any] | None = None, context: Any = None) -> dict[str, Any]:
    import boto3

    from backend.connectors.graph.connector import GraphConnector
    from services.secrets import SecretsClient

    event = event or {}
    sites = json.loads(os.environ.get("APHRODITE_SHAREPOINT_SITES", "[]"))
    bucket = os.environ["APHRODITE_CRAWL_BUCKET"]
    if not sites:
        logger.info("no SharePoint sites configured; nothing to crawl")
        return {"crawled": False, "reason": "no sites configured"}

    secrets = SecretsClient(boto3.client("secretsmanager", region_name=REGION))
    graph = GraphConnector(secrets)
    storage = S3CrawlStorage(
        boto3.client("s3", region_name=REGION),
        bucket,
        kms_key_id=os.environ.get("APHRODITE_CRAWL_KMS_KEY_ID"),
    )
    crawler = SharePointCrawler(graph=graph, storage=storage, sites=sites)

    stats = crawler.crawl(deadline=_deadline_from(context))
    logger.info("crawl complete", extra={"extra_fields": stats.as_dict()})

    # Stopped on the runtime budget with progress made → continue in a fresh run
    # (the saved per-page cursor resumes where we left off). Defer ingestion until
    # the crawl actually completes.
    if stats.stopped_early and stats.pages > 0:
        continued = _self_continue(boto3, event)
        return {"crawled": True, "stats": stats.as_dict(), "continued": continued}
    if stats.stopped_early:  # pages == 0 → a single page exceeds the budget
        logger.warning("crawler made no page progress within the budget; not self-continuing")

    ingestion = _start_ingestion(boto3, stats)
    return {"crawled": True, "stats": stats.as_dict(), "ingestion": ingestion}


def _deadline_from(context: Any) -> float | None:
    """Monotonic timestamp to stop by, leaving a safety margin before the Lambda
    deadline. None when there's no usable context (local/tests) ⇒ no budget."""
    try:
        remaining_ms = context.get_remaining_time_in_millis()
    except Exception:  # noqa: BLE001 - no/unsupported context => run unbounded
        return None
    return time.monotonic() + remaining_ms / 1000.0 - _DEADLINE_SAFETY_SECONDS


def _self_continue(boto3: Any, event: dict[str, Any]) -> bool:
    """Async re-invoke this function to continue the backfill from the saved cursor."""
    fn = os.environ.get("APHRODITE_SELF_FUNCTION")
    if not fn:
        logger.warning("APHRODITE_SELF_FUNCTION unset; cannot self-continue the crawl")
        return False
    n = int(event.get("continues", 0)) + 1
    if n > _MAX_CONTINUES:
        logger.warning("crawler self-continue cap reached", extra={"extra_fields": {"n": n}})
        return False
    boto3.client("lambda", region_name=REGION).invoke(
        FunctionName=fn,
        InvocationType="Event",
        Payload=json.dumps({"continues": n}).encode("utf-8"),
    )
    logger.info("crawler self-continuing", extra={"extra_fields": {"continues": n}})
    return True


def _start_ingestion(boto3: Any, stats: Any) -> str | None:
    """Kick a KB re-ingestion so the crawl's changes are indexed."""
    kb_id = os.environ.get("APHRODITE_SHAREPOINT_KB_ID")
    ds_id = os.environ.get("APHRODITE_SHAREPOINT_DATA_SOURCE_ID")
    if not (kb_id and ds_id):
        return None
    # Nothing changed ⇒ skip the ingestion job (saves an embedding pass).
    if not (stats.downloaded or stats.deleted):
        return None
    kb_region = os.environ.get("APHRODITE_KB_REGION", REGION)
    agent = boto3.client("bedrock-agent", region_name=kb_region)
    job = agent.start_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id)
    return (job.get("ingestionJob") or {}).get("ingestionJobId")
