"""Tests for the S3 writer: key layout, SSE-KMS, manifest content."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from typing import Any

from backend.lambdas.aml_export.storage import AmlExportStorage


class FakeS3:
    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []

    def put_object(self, **kwargs: Any) -> None:
        self.puts.append(kwargs)


class FakeS3WithManifests:
    """Supports the list + get used by ``load_prior_matter_numbers``."""

    def __init__(self, manifests: dict[str, dict[str, Any]]) -> None:
        self.manifests = manifests

    def get_paginator(self, _name: str) -> Any:
        keys = list(self.manifests)

        class _Paginator:
            def paginate(self, **_kw: Any):
                yield {"Contents": [{"Key": k} for k in keys]}

        return _Paginator()

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        return {"Body": io.BytesIO(json.dumps(self.manifests[Key]).encode())}


def test_put_export_writes_workbook_and_manifest_under_month_prefix():
    s3 = FakeS3()
    storage = AmlExportStorage(s3=s3, bucket="ht-outputs", prefix="aml")
    now = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)

    result = {"parties": [{}], "excluded": [], "exceptions": [], "summary": {}}
    keys = storage.put_export(b"workbook-bytes", result, now=now)

    assert keys == {
        "workbook": "aml/2026-07/AML_Name_Export_2026-07-01.xlsx",
        "manifest": "aml/2026-07/manifest.json",
    }
    assert [p["Key"] for p in s3.puts] == [keys["workbook"], keys["manifest"]]


def test_put_export_applies_sse_kms_when_key_configured():
    s3 = FakeS3()
    storage = AmlExportStorage(s3=s3, bucket="ht-outputs", kms_key_id="arn:aws:kms:key/abc")
    now = datetime(2026, 7, 1, tzinfo=UTC)

    storage.put_export(
        b"bytes", {"parties": [], "excluded": [], "exceptions": [], "summary": {}}, now=now
    )

    for put in s3.puts:
        assert put["ServerSideEncryption"] == "aws:kms"
        assert put["SSEKMSKeyId"] == "arn:aws:kms:key/abc"


def test_put_export_omits_sse_kms_when_no_key_configured():
    s3 = FakeS3()
    storage = AmlExportStorage(s3=s3, bucket="ht-outputs")
    now = datetime(2026, 7, 1, tzinfo=UTC)

    storage.put_export(
        b"bytes", {"parties": [], "excluded": [], "exceptions": [], "summary": {}}, now=now
    )

    for put in s3.puts:
        assert "ServerSideEncryption" not in put
        assert "SSEKMSKeyId" not in put


def test_manifest_records_counts_and_no_pii():
    s3 = FakeS3()
    storage = AmlExportStorage(s3=s3, bucket="ht-outputs")
    now = datetime(2026, 7, 1, tzinfo=UTC)
    result = {
        "parties": [{"full_legal_name": "Jane Doe"}, {"full_legal_name": "John Roe"}],
        "excluded": [{"reason": "Marked DO NOT USE"}],
        "excluded_parties": [{"full_legal_name": "Aphrodite"}],
        "exceptions": [{"issue": "Missing date of birth"}],
        "matter_numbers": ["240001", "240002"],
        "summary": {
            "matters_by_type": {"Estate": 1},
            "missing_dob": 1,
            "conflicting_matter_numbers": 0,
            "roster_incomplete_count": 3,
            "unverified_matters": 2,
        },
    }

    storage.put_export(b"bytes", result, now=now)

    manifest_put = next(p for p in s3.puts if p["Key"].endswith("manifest.json"))
    manifest = json.loads(manifest_put["Body"])
    assert manifest["parties_count"] == 2
    assert manifest["excluded_count"] == 1
    assert manifest["excluded_parties_count"] == 1
    assert manifest["exceptions_count"] == 1
    assert manifest["roster_incomplete_count"] == 3
    assert manifest["unverified_matters"] == 2
    assert manifest["matters_by_type"] == {"Estate": 1}
    assert manifest["matter_numbers"] == ["240001", "240002"]
    assert manifest["review_status"] == "pending_review"
    # No PII: neither a client name nor the excluded company name appears.
    assert "Jane Doe" not in json.dumps(manifest)
    assert "Aphrodite" not in json.dumps(manifest)


def test_load_prior_matter_numbers_picks_most_recent_before_now():
    manifests = {
        "aml/2026-06/manifest.json": {
            "generated_at": "2026-06-01T00:00:00+00:00",
            "matter_numbers": ["240001", "240002"],
        },
        "aml/2026-07/manifest.json": {
            "generated_at": "2026-07-01T00:00:00+00:00",
            "matter_numbers": ["240001"],
        },
    }
    storage = AmlExportStorage(s3=FakeS3WithManifests(manifests), bucket="ht-outputs")
    # A run mid-July uses the July baseline (most recent before now).
    assert storage.load_prior_matter_numbers(datetime(2026, 7, 15, tzinfo=UTC)) == {"240001"}
    # A run mid-June (the July manifest is in the future) falls back to June's set.
    assert storage.load_prior_matter_numbers(datetime(2026, 6, 15, tzinfo=UTC)) == {
        "240001",
        "240002",
    }
    # No prior manifest at all ⇒ empty (no drop check).
    empty = AmlExportStorage(s3=FakeS3WithManifests({}), bucket="ht-outputs")
    assert empty.load_prior_matter_numbers(datetime(2026, 7, 15, tzinfo=UTC)) == set()
