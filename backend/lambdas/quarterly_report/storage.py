"""Write generated report drafts to the immutable S3 outputs bucket.

Layout is period-partitioned so each quarter's draft lives at its own key and the
four quarters of a year never collide::

    quarterly/{matter_number}/{YYYY-Qn}/cover-letter.docx
    quarterly/{matter_number}/{YYYY-Qn}/trust-ledger.pdf
    quarterly/{matter_number}/{YYYY-Qn}/manifest.json

Re-generating the same quarter overwrites these keys, but the bucket has S3
versioning + Object Lock, so every prior generation is retained immutably for the
audit trail (D7). The ``manifest.json`` records provenance and a ``review_status``
the portal/reviewer can use.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .model import ReportModel

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_PDF_MIME = "application/pdf"
_JSON_MIME = "application/json"


@dataclass
class ReportStorage:
    """Puts the three per-report objects to S3 (server-side encrypted)."""

    s3: Any  # boto3 S3 client
    bucket: str
    kms_key_id: str | None = None

    def prefix(self, model: ReportModel) -> str:
        return f"quarterly/{model.matter_number}/{model.period.code}"

    def put_report(
        self, model: ReportModel, docx: bytes, pdf: bytes, *, generated_at: str
    ) -> dict[str, str]:
        """Write the letter, ledger, and manifest; return their keys."""
        prefix = self.prefix(model)
        docx_key = f"{prefix}/cover-letter.docx"
        pdf_key = f"{prefix}/trust-ledger.pdf"
        manifest_key = f"{prefix}/manifest.json"

        self._put(docx_key, docx, _DOCX_MIME)
        self._put(pdf_key, pdf, _PDF_MIME)
        manifest = _manifest(
            model, generated_at, {"cover_letter": docx_key, "trust_ledger": pdf_key}
        )
        self._put(manifest_key, json.dumps(manifest, indent=2, default=str).encode(), _JSON_MIME)

        return {"cover_letter": docx_key, "trust_ledger": pdf_key, "manifest": manifest_key}

    def _put(self, key: str, body: bytes, content_type: str) -> None:
        extra: dict[str, Any] = {"ContentType": content_type}
        if self.kms_key_id:
            extra["ServerSideEncryption"] = "aws:kms"
            extra["SSEKMSKeyId"] = self.kms_key_id
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=body, **extra)


def _manifest(model: ReportModel, generated_at: str, files: dict[str, str]) -> dict[str, Any]:
    return {
        "matter_number": model.matter_number,
        "trust_name": model.trust_name,
        "period": model.period.code,
        "period_start": model.period.start.isoformat(),
        "period_end": model.period.end.isoformat(),
        "generated_at": generated_at,
        "review_status": "pending_review",
        "cash_balance": _num(model.cash_balance),
        "total_funds_in": _num(model.total_funds_in),
        "total_funds_out": _num(model.total_funds_out),
        "investment_pending": model.investment_pending,
        "total_assets": _num(model.total_assets),
        "asset_breakdown": {k: _num(v) for k, v in model.asset_breakdown.items()},
        "unclassified_accounts": list(model.unclassified_accounts),
        "stale_valuations": [
            {"account": name, "as_at": as_at} for name, as_at in model.stale_valuations
        ],
        "real_property_as_at": model.real_property_as_at,
        "ledger_row_count": len(model.ledger),
        "source_refs": model.source_refs,
        "files": files,
    }


def _num(value: Decimal | None) -> float | None:
    return None if value is None else float(value)
