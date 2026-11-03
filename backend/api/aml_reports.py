"""List and download generated AML name-export workbooks from the outputs bucket.

Backs the portal's AML Export tab. Reading is via the per-run ``manifest.json``
written by the monthly generator (D6, ``backend/lambdas/aml_export/storage.py``),
so the list carries generation counts and review status without re-opening the
workbook. Downloads are short-lived presigned URLs, scoped to the ``aml/``
prefix so this endpoint can never hand out an arbitrary object.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from services.logging import get_logger

logger = get_logger(__name__)

_PREFIX = "aml/"
_MANIFEST_SUFFIX = "/manifest.json"
_DOWNLOAD_TTL_SECONDS = 300


class AmlExportAccessError(Exception):
    """Raised when a requested key is outside the allowed AML export prefix."""


@dataclass
class AmlExportsService:
    s3: Any  # boto3 S3 client
    bucket: str

    def list(self) -> list[dict[str, Any]]:
        """Return every run's manifest, newest month first."""
        exports: list[dict[str, Any]] = []
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=_PREFIX):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith(_MANIFEST_SUFFIX):
                    manifest = self._read_manifest(key)
                    if manifest:
                        manifest.setdefault("period", _period_from_key(key))
                        exports.append(manifest)
        exports.sort(key=lambda r: str(r.get("period") or ""), reverse=True)
        return exports

    def download_url(self, key: str) -> str:
        """Presigned GET URL for an export object; only within ``aml/``."""
        if not key.startswith(_PREFIX) or ".." in key:
            raise AmlExportAccessError(f"key outside AML export prefix: {key}")
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


def _period_from_key(key: str) -> str | None:
    """Derive ``YYYY-MM`` from ``aml/{YYYY-MM}/manifest.json``."""
    parts = key.split("/")
    return parts[1] if len(parts) > 2 else None
