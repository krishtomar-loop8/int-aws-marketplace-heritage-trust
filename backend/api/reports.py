"""List and download generated report drafts from the outputs bucket.

Backs the portal's Quarterly Reports tab. Reading is via the per-report
``manifest.json`` written by the generator (D5), so the list carries the matter,
period, status, and figures without re-parsing the documents. Downloads are
short-lived presigned URLs, scoped to the ``quarterly/`` prefix so this endpoint
can never hand out an arbitrary object.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from services.logging import get_logger

logger = get_logger(__name__)

_PREFIX = "quarterly/"
_MANIFEST_SUFFIX = "/manifest.json"
_DOWNLOAD_TTL_SECONDS = 300


class ReportAccessError(Exception):
    """Raised when a requested key is outside the allowed report prefix."""


@dataclass
class ReportsService:
    s3: Any  # boto3 S3 client
    bucket: str

    def list(self) -> list[dict[str, Any]]:
        """Return every report's manifest, newest period first per matter."""
        reports: list[dict[str, Any]] = []
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=_PREFIX):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith(_MANIFEST_SUFFIX):
                    manifest = self._read_manifest(key)
                    if manifest:
                        reports.append(manifest)
        reports.sort(
            key=lambda r: (str(r.get("matter_number")), str(r.get("period"))), reverse=True
        )
        return reports

    def download_url(self, key: str) -> str:
        """Presigned GET URL for a report object; only within ``quarterly/``."""
        if not key.startswith(_PREFIX) or ".." in key:
            raise ReportAccessError(f"key outside reports prefix: {key}")
        return self.s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=_DOWNLOAD_TTL_SECONDS,
        )

    def _read_manifest(self, key: str) -> dict[str, Any] | None:
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=key)
            return json.loads(obj["Body"].read())
        except Exception:  # noqa: BLE001 - a malformed/missing manifest shouldn't drop the whole list
            logger.warning("skipping unreadable manifest", extra={"extra_fields": {"key": key}})
            return None
