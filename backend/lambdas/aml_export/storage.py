"""Write the monthly AML export workbook to the immutable S3 outputs bucket.

Layout is month-partitioned so re-running mid-month overwrites the same
objects while a new month gets a fresh key::

    aml/{YYYY-MM}/AML_Name_Export_{YYYY-MM-DD}.xlsx
    aml/{YYYY-MM}/manifest.json

The bucket has S3 versioning + Object Lock, so every prior generation is
retained immutably for the audit trail (D7). The ``manifest.json`` records
provenance and row counts only — never PII — so a reviewer can sanity
-check the run before the file is uploaded to the Sanctions Screener.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_JSON_MIME = "application/json"


@dataclass
class AmlExportStorage:
    """Puts the workbook + manifest to S3 (server-side encrypted)."""

    s3: Any  # boto3 S3 client
    bucket: str
    prefix: str = "aml"
    kms_key_id: str | None = None

    def month_prefix(self, now: datetime) -> str:
        return f"{self.prefix}/{now.strftime('%Y-%m')}"

    def put_export(
        self, workbook_bytes: bytes, result: dict[str, list[dict[str, Any]]], *, now: datetime
    ) -> dict[str, str]:
        """Write the workbook and manifest for ``now``'s run; return their keys."""
        month_prefix = self.month_prefix(now)
        workbook_key = f"{month_prefix}/AML_Name_Export_{now.strftime('%Y-%m-%d')}.xlsx"
        manifest_key = f"{month_prefix}/manifest.json"

        self._put(workbook_key, workbook_bytes, _XLSX_MIME)
        manifest = _manifest(result, now.isoformat(), workbook_key)
        self._put(manifest_key, json.dumps(manifest, indent=2, default=str).encode(), _JSON_MIME)

        return {"workbook": workbook_key, "manifest": manifest_key}

    def _put(self, key: str, body: bytes, content_type: str) -> None:
        extra: dict[str, Any] = {"ContentType": content_type}
        if self.kms_key_id:
            extra["ServerSideEncryption"] = "aws:kms"
            extra["SSEKMSKeyId"] = self.kms_key_id
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=body, **extra)

    def load_prior_matter_numbers(self, now: datetime) -> set[str]:
        """Return the matter-number set from the most recent prior run's manifest.

        Used to flag matters that dropped since the last export. Reads every
        ``{prefix}/*/manifest.json`` and picks the newest whose ``generated_at``
        precedes ``now`` (so the current run's own manifest is ignored). Any read
        failure is tolerated — a missing baseline simply means no drop check."""
        cutoff = now.isoformat()
        latest_ts = ""
        latest: dict[str, Any] | None = None
        try:
            paginator = self.s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=f"{self.prefix}/"):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    if not key.endswith("/manifest.json"):
                        continue
                    try:
                        body = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
                        manifest = json.loads(body)
                    except Exception:
                        continue
                    ts = str(manifest.get("generated_at") or "")
                    if ts and ts < cutoff and ts > latest_ts:
                        latest_ts, latest = ts, manifest
        except Exception:
            return set()
        return set(latest.get("matter_numbers", [])) if latest else set()


def _manifest(result: dict[str, Any], generated_at: str, workbook_key: str) -> dict[str, Any]:
    """Provenance + counts only — never PII (see the no-PII test)."""
    summary = result.get("summary", {})
    return {
        "generated_at": generated_at,
        "review_status": "pending_review",
        "parties_count": len(result.get("parties", [])),
        "matters_by_type": summary.get("matters_by_type", {}),
        "excluded_count": len(result.get("excluded", [])),
        "excluded_parties_count": len(result.get("excluded_parties", [])),
        "exceptions_count": len(result.get("exceptions", [])),
        "roster_incomplete_count": summary.get("roster_incomplete_count", 0),
        "missing_dob": summary.get("missing_dob", 0),
        "unverified_matters": summary.get("unverified_matters", 0),
        "conflicting_matter_numbers": summary.get("conflicting_matter_numbers", 0),
        # Digits-only matter keys (no embedded surnames) — used to detect matters
        # that drop out of a later run. Identifiers, not PII.
        "matter_numbers": result.get("matter_numbers", []),
        "workbook_key": workbook_key,
    }
