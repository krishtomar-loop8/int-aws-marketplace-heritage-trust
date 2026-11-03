"""Unit tests for the S3 report storage writer. Fake S3 client; no AWS."""

from __future__ import annotations

import json

from backend.lambdas.quarterly_report.storage import ReportStorage
from backend.lambdas.quarterly_report.tests.test_render import _model


class FakeS3:
    def __init__(self) -> None:
        self.puts: list[dict] = []

    def put_object(self, **kwargs):
        self.puts.append(kwargs)
        return {}


def test_put_report_writes_three_period_partitioned_objects():
    s3 = FakeS3()
    storage = ReportStorage(s3=s3, bucket="ht-outputs")
    keys = storage.put_report(_model(), b"DOCX", b"PDF", generated_at="2026-05-11T12:00:00Z")

    assert keys["cover_letter"] == "quarterly/100001/2026-Q1/cover-letter.docx"
    assert keys["trust_ledger"] == "quarterly/100001/2026-Q1/trust-ledger.pdf"
    assert keys["manifest"] == "quarterly/100001/2026-Q1/manifest.json"

    by_key = {p["Key"]: p for p in s3.puts}
    assert set(by_key) == set(keys.values())
    assert by_key[keys["cover_letter"]]["Body"] == b"DOCX"
    assert by_key[keys["cover_letter"]]["ContentType"].endswith("wordprocessingml.document")
    assert by_key[keys["trust_ledger"]]["ContentType"] == "application/pdf"


def test_manifest_captures_provenance_and_review_status():
    s3 = FakeS3()
    ReportStorage(s3=s3, bucket="ht-outputs").put_report(
        _model(), b"DOCX", b"PDF", generated_at="2026-05-11T12:00:00Z"
    )
    manifest_put = next(p for p in s3.puts if p["Key"].endswith("manifest.json"))
    manifest = json.loads(manifest_put["Body"])

    assert manifest["matter_number"] == "100001"
    assert manifest["period"] == "2026-Q1"
    assert manifest["review_status"] == "pending_review"
    assert manifest["cash_balance"] == 8450.25
    assert manifest["investment_pending"] is True
    assert manifest["total_assets"] is None
    assert manifest["files"]["cover_letter"].endswith("cover-letter.docx")
    # Assets Held breakdown + review flags surfaced for the portal.
    assert manifest["asset_breakdown"]["Real Property"] == 500000.0
    assert manifest["unclassified_accounts"] == ["Special Holding 9001"]
    assert manifest["stale_valuations"] == [
        {"account": "Assets Under Administration - Real Property", "as_at": "2024-01-10"}
    ]
    assert manifest["real_property_as_at"] == "2024-01-10"


def test_sse_kms_applied_when_key_configured():
    s3 = FakeS3()
    ReportStorage(s3=s3, bucket="ht-outputs", kms_key_id="key-123").put_report(
        _model(), b"D", b"P", generated_at="2026-05-11T12:00:00Z"
    )
    for put in s3.puts:
        assert put["ServerSideEncryption"] == "aws:kms"
        assert put["SSEKMSKeyId"] == "key-123"


def test_no_sse_header_when_no_key():
    s3 = FakeS3()
    ReportStorage(s3=s3, bucket="ht-outputs").put_report(
        _model(), b"D", b"P", generated_at="2026-05-11T12:00:00Z"
    )
    assert all("ServerSideEncryption" not in p for p in s3.puts)
