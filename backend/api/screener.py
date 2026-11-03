"""Sanctions screening for the portal's Sanction Screener tab.

Ported behaviour from the inherited WorkSmarter tool, adapted to the POC:

- ``screen`` parses an uploaded CSV/Excel name list in memory, fuzzy-matches
  every name against the UN / SEMA / Public Safety Canada watchlists, and
  returns only the flagged matches (clean names never leave the server). The
  uploaded bytes and the raw match detail are discarded when the call returns.
- ``certificate`` (after the analyst adjudicates each match) renders the
  Certificate of Search PDF and writes it — plus a PII-free manifest — to the
  immutable outputs bucket under ``screener/``. That certificate is the audit
  record; nothing else is persisted server-side.
- ``list`` / ``download_url`` back the "past certificates" view (manifest list
  + short-lived presigned download, scoped to the ``screener/`` prefix).

The watchlist bundle is loaded once per cold start from the reference S3 bucket
(kept current by the monthly sanctions-refresh Lambda) and falls back to the
copy packaged in the Lambda zip if S3 is unavailable — so the tab always works,
just with staler data until the first refresh runs.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.screener.screening import build_summary, process_and_filter_results
from services.logging import get_logger

logger = get_logger(__name__)

_PREFIX = "screener/"
_MANIFEST_SUFFIX = "/manifest.json"
_DOWNLOAD_TTL_SECONDS = 300
_ALLOWED_EXT = (".csv", ".xlsx", ".xls")
_PDF_MIME = "application/pdf"
_JSON_MIME = "application/json"
_ORG_NAME = "Aphrodite"

# Packaged fallback bundle shipped in the Lambda zip: backend/screener/data/…
_PACKAGED_BUNDLE = (
    Path(__file__).resolve().parent.parent / "screener" / "data" / "sanctions_bundle.json"
)

# Source-list labels applied when flattening the bundle into matcher entries.
_SOURCE_LABELS = (
    ("un_list", "UN"),
    ("sema_list", "Canada-SEMA"),
    ("ps_list", "Public Safety Canada"),
)


class ScreenerError(Exception):
    """Bad screening input (unsupported/empty file, or matches still pending)."""


class ScreenerAccessError(Exception):
    """Requested object key is outside the screener prefix."""


@dataclass
class ScreenerService:
    """Screen uploads and produce/serve Certificates of Search."""

    s3: Any  # boto3 S3 client
    outputs_bucket: str
    sanctions_bucket: str | None = None
    sanctions_key: str = "sanctions/bundle.json"
    prefix: str = "screener"
    kms_key_id: str | None = None
    matcher: Any = None  # built lazily on first screen (or injected in tests)

    # -- screening ----------------------------------------------------------

    def screen(self, content: bytes, filename: str) -> dict[str, Any]:
        """Parse + screen an uploaded file; return summary + flagged matches."""
        if not content:
            raise ScreenerError("Empty file")
        if not filename.lower().endswith(_ALLOWED_EXT):
            raise ScreenerError("Unsupported file type. Use CSV or Excel (.xlsx/.xls)")

        from backend.screener import preprocessor

        try:
            persons = preprocessor.process_file(content, filename)
            flagged, total = process_and_filter_results(persons, self._get_matcher())
            return {
                **build_summary(total, flagged),
                "file_hash": hashlib.sha256(content).hexdigest(),
                "flagged_results": flagged,
            }
        except ValueError as exc:  # unrecognised format / no names
            raise ScreenerError(str(exc)) from exc
        finally:
            preprocessor.clear_session()

    # -- certificate --------------------------------------------------------

    def certificate(
        self, payload: dict[str, Any], actor: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Render the Certificate of Search PDF, store it, return a download URL."""
        results = payload.get("results") or {}
        file_hash = payload.get("file_hash") or ""
        metadata = dict(payload.get("metadata") or {})

        flagged = results.get("flagged_results", [])
        pending = sum(1 for r in flagged if r.get("status") == "pending")
        if pending:
            raise ScreenerError(f"{pending} match(es) still pending review")

        reviewed_by = (actor or {}).get("email")
        if reviewed_by:
            metadata.setdefault("reviewed_by", reviewed_by)

        from backend.screener import reporting

        pdf = reporting.generate_certificate_of_search(
            results=results,
            file_hash=file_hash or "N/A",
            metadata=metadata,
            organization_name=_ORG_NAME,
        )
        now = datetime.now(UTC)
        keys = self._store(pdf, results, file_hash, reviewed_by, now)
        return {
            "url": self.download_url(keys["certificate"]),
            "key": keys["certificate"],
            "manifest": keys["manifest"],
        }

    # -- audit list ---------------------------------------------------------

    def list(self) -> list[dict[str, Any]]:
        """Every stored certificate's manifest, newest first."""
        items: list[dict[str, Any]] = []
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.outputs_bucket, Prefix=_PREFIX):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith(_MANIFEST_SUFFIX):
                    manifest = self._read_manifest(key)
                    if manifest:
                        items.append(manifest)
        items.sort(key=lambda r: str(r.get("generated_at") or ""), reverse=True)
        return items

    def download_url(self, key: str) -> str:
        """Presigned GET URL for a stored certificate/manifest; screener/ only."""
        if not key.startswith(_PREFIX) or ".." in key:
            raise ScreenerAccessError(f"key outside screener prefix: {key}")
        return self.s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.outputs_bucket, "Key": key},
            ExpiresIn=_DOWNLOAD_TTL_SECONDS,
        )

    def delete(self, key: str) -> int:
        """Soft-delete a certificate run (the cert PDF + its manifest).

        The outputs bucket is versioned + Object-Lock (GOVERNANCE), so ``delete_object``
        without a VersionId writes a *delete marker*: the run disappears from
        :meth:`list` while the locked versions are retained for the audit trail (no
        Object-Lock bypass). ``key`` is any object in the run (the certificate key);
        we remove everything under its run folder. Returns the number of objects hidden.
        """
        if not key.startswith(_PREFIX) or ".." in key:
            raise ScreenerAccessError(f"key outside screener prefix: {key}")
        run_prefix = key.rsplit("/", 1)[0] + "/"
        # Guard against deleting a whole month or the entire prefix — a real run key is
        # screener/{YYYY-MM}/{run}/{file}, so its run folder has at least three slashes.
        if run_prefix.count("/") < 3:
            raise ScreenerAccessError(f"not a certificate object key: {key}")
        removed = 0
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.outputs_bucket, Prefix=run_prefix):
            for obj in page.get("Contents", []):
                self.s3.delete_object(Bucket=self.outputs_bucket, Key=obj["Key"])
                removed += 1
        logger.info(
            "soft-deleted screener certificate",
            extra={"extra_fields": {"run": run_prefix, "objects": removed}},
        )
        return removed

    # -- internals ----------------------------------------------------------

    def _get_matcher(self) -> Any:
        if self.matcher is None:
            from backend.screener.matcher import SanctionMatcher

            bundle = self._load_bundle()
            self.matcher = SanctionMatcher(sanctions_data=_entries_from_bundle(bundle))
            logger.info(
                "sanctions matcher built",
                extra={"extra_fields": {"entries": len(self.matcher.entries)}},
            )
        return self.matcher

    def _load_bundle(self) -> dict[str, Any]:
        if self.sanctions_bucket:
            try:
                obj = self.s3.get_object(Bucket=self.sanctions_bucket, Key=self.sanctions_key)
                bundle = json.loads(obj["Body"].read())
                logger.info(
                    "sanctions bundle loaded from S3",
                    extra={
                        "extra_fields": {
                            "bucket": self.sanctions_bucket,
                            "generated_at": bundle.get("generated_at"),
                        }
                    },
                )
                return bundle
            except Exception:  # noqa: BLE001 - fall back to the packaged copy
                logger.warning(
                    "S3 sanctions bundle unavailable; using packaged fallback",
                    extra={"extra_fields": {"bucket": self.sanctions_bucket}},
                )
        bundle = json.loads(_PACKAGED_BUNDLE.read_text())
        logger.info(
            "sanctions bundle loaded from package",
            extra={"extra_fields": {"generated_at": bundle.get("generated_at")}},
        )
        return bundle

    def _store(
        self,
        pdf: bytes,
        results: dict[str, Any],
        file_hash: str,
        reviewed_by: str | None,
        now: datetime,
    ) -> dict[str, str]:
        short = (file_hash[:8] or "nohash").lower()
        run = f"{self.prefix}/{now.strftime('%Y-%m')}/{now.strftime('%Y-%m-%dT%H-%M-%SZ')}_{short}"
        cert_key = f"{run}/Certificate_of_Search_{now.strftime('%Y-%m-%d')}.pdf"
        manifest_key = f"{run}/manifest.json"

        self._put(cert_key, pdf, _PDF_MIME)
        manifest = _manifest(results, file_hash, reviewed_by, now.isoformat(), cert_key)
        self._put(manifest_key, json.dumps(manifest, indent=2, default=str).encode(), _JSON_MIME)
        return {"certificate": cert_key, "manifest": manifest_key}

    def _put(self, key: str, body: bytes, content_type: str) -> None:
        extra: dict[str, Any] = {"ContentType": content_type}
        if self.kms_key_id:
            extra["ServerSideEncryption"] = "aws:kms"
            extra["SSEKMSKeyId"] = self.kms_key_id
        self.s3.put_object(Bucket=self.outputs_bucket, Key=key, Body=body, **extra)

    def _read_manifest(self, key: str) -> dict[str, Any] | None:
        try:
            obj = self.s3.get_object(Bucket=self.outputs_bucket, Key=key)
            return json.loads(obj["Body"].read())
        except Exception:  # noqa: BLE001 - one bad manifest shouldn't drop the list
            logger.warning(
                "skipping unreadable screener manifest", extra={"extra_fields": {"key": key}}
            )
            return None


def _entries_from_bundle(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten the three watchlists into a single list with source labels."""
    entries: list[dict[str, Any]] = []
    for key, label in _SOURCE_LABELS:
        for entry in bundle.get(key, []):
            tagged = dict(entry)
            tagged["_source_label"] = label
            entries.append(tagged)
    return entries


def _manifest(
    results: dict[str, Any],
    file_hash: str,
    reviewed_by: str | None,
    generated_at: str,
    cert_key: str,
) -> dict[str, Any]:
    """Provenance record for a certificate — counts + hash + actor, never names."""
    flagged = results.get("flagged_results", [])
    confirmed = sum(1 for r in flagged if r.get("status") == "confirmed")
    dismissed = sum(1 for r in flagged if r.get("status") == "dismissed")
    return {
        "generated_at": generated_at,
        "review_status": "confirmed_matches" if confirmed else "cleared",
        "total_screened": results.get("total_screened", 0),
        "initial_matches": len(flagged),
        "confirmed_count": confirmed,
        "dismissed_count": dismissed,
        "file_hash": file_hash,
        "reviewed_by": reviewed_by,
        "certificate_key": cert_key,
    }
