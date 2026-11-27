"""Tests for the ScreenerService (screen + certificate + list) with a fake S3.

Exercises the real preprocessor, matcher (tiny in-memory watchlist) and
reportlab certificate rendering — no AWS.
"""

import io
import json
from datetime import UTC, datetime

import pytest

from backend.api.screener import (
    ScreenerAccessError,
    ScreenerError,
    ScreenerService,
    _is_bundle_stale,
)
from backend.screener.matcher import SanctionMatcher


class FakeS3:
    """Minimal in-memory stand-in for the boto3 S3 client."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, Bucket, Key, Body, **kwargs):  # noqa: N803 - boto3 kwarg names
        self.objects[Key] = Body

    def delete_object(self, Bucket, Key):  # noqa: N803 - soft delete = drop current version
        self.objects.pop(Key, None)

    def get_object(self, Bucket, Key):  # noqa: N803
        return {"Body": io.BytesIO(self.objects[Key])}

    def generate_presigned_url(self, operation, Params, ExpiresIn):  # noqa: N803
        return f"https://example.test/{Params['Key']}"

    def get_paginator(self, name):
        objects = [{"Key": k} for k in self.objects]

        class _Paginator:
            def paginate(self, Bucket, Prefix=""):  # noqa: N803
                yield {"Contents": [o for o in objects if o["Key"].startswith(Prefix)]}

        return _Paginator()


def _service(mock_sanctions_data, **kwargs) -> ScreenerService:
    matcher = SanctionMatcher(sanctions_data=mock_sanctions_data)
    return ScreenerService(s3=FakeS3(), outputs_bucket="outputs", matcher=matcher, **kwargs)


def test_screen_flags_match_and_hides_clean_names(mock_sanctions_data):
    svc = _service(mock_sanctions_data)
    csv = b"name,dob\nJohn Doe,1980-01-01\nClean Person,1990-01-01\n"

    result = svc.screen(csv, "list.csv")

    assert result["total_screened"] == 2
    assert result["total_matches"] >= 1
    assert "file_hash" in result
    names = [r["client_name"] for r in result["flagged_results"]]
    assert "John Doe" in names
    # Exception-only reporting: a clean name is never returned.
    assert "Clean Person" not in names


def test_screen_rejects_unsupported_and_empty(mock_sanctions_data):
    svc = _service(mock_sanctions_data)
    with pytest.raises(ScreenerError):
        svc.screen(b"anything", "notes.txt")
    with pytest.raises(ScreenerError):
        svc.screen(b"", "list.csv")


def test_certificate_stores_pdf_and_pii_free_manifest(mock_sanctions_data):
    svc = _service(mock_sanctions_data)
    payload = {
        "file_hash": "abc123def456abc1",
        "results": {
            "total_screened": 2,
            "flagged_results": [
                {
                    "client_name": "John Doe",
                    "matched_name": "John Doe",
                    "score": 100,
                    "source_list": "UN",
                    "status": "dismissed",
                }
            ],
        },
        "metadata": {"original_filename": "list.csv"},
    }

    out = svc.certificate(payload, actor={"email": "analyst@aphrodite.example"})

    assert out["key"].startswith("screener/")
    assert out["key"].endswith(".pdf")
    assert out["url"].startswith("https://")
    assert any(k.endswith(".pdf") for k in svc.s3.objects)

    manifest = json.loads(svc.s3.objects[out["manifest"]])
    assert manifest["reviewed_by"] == "analyst@aphrodite.example"
    assert manifest["dismissed_count"] == 1
    assert manifest["review_status"] == "cleared"
    # The manifest carries counts only — never client names.
    assert "John Doe" not in svc.s3.objects[out["manifest"]].decode()

    listed = svc.list()
    assert len(listed) == 1
    assert listed[0]["certificate_key"] == out["key"]


def test_certificate_rejects_pending_matches(mock_sanctions_data):
    svc = _service(mock_sanctions_data)
    with pytest.raises(ScreenerError):
        svc.certificate({"results": {"flagged_results": [{"status": "pending"}]}})


def test_download_url_rejects_foreign_key(mock_sanctions_data):
    svc = _service(mock_sanctions_data)
    with pytest.raises(ScreenerAccessError):
        svc.download_url("aml/2026-07/AML_Name_Export.xlsx")


def test_delete_soft_removes_the_run(mock_sanctions_data):
    svc = _service(mock_sanctions_data)
    payload = {
        "file_hash": "abc123def456abc1",
        "results": {
            "total_screened": 1,
            "flagged_results": [
                {
                    "client_name": "X",
                    "matched_name": "X",
                    "score": 100,
                    "source_list": "UN",
                    "status": "dismissed",
                }
            ],
        },
    }
    out = svc.certificate(payload)
    assert len(svc.list()) == 1

    removed = svc.delete(out["key"])

    assert removed == 2  # certificate PDF + manifest
    assert svc.list() == []


def test_delete_rejects_foreign_or_toplevel_keys(mock_sanctions_data):
    svc = _service(mock_sanctions_data)
    for bad in ("aml/2026-07/x.xlsx", "screener/", "screener/2026-07/"):
        with pytest.raises(ScreenerAccessError):
            svc.delete(bad)


def test_fresh_bundle_is_not_stale():
    now = datetime(2026, 6, 1, tzinfo=UTC)
    assert _is_bundle_stale("2026-05-20T00:00:00+00:00", now=now) is False


def test_bundle_past_threshold_is_stale():
    now = datetime(2026, 6, 1, tzinfo=UTC)
    assert _is_bundle_stale("2026-04-01T00:00:00+00:00", now=now) is True  # 61 days


def test_bundle_exactly_at_threshold_boundary():
    now = datetime(2026, 6, 1, tzinfo=UTC)
    assert _is_bundle_stale("2026-04-17T00:00:00+00:00", now=now) is False  # 45 days
    assert _is_bundle_stale("2026-04-16T00:00:00+00:00", now=now) is True  # 46 days


def test_missing_generated_at_is_treated_as_stale():
    assert _is_bundle_stale(None, now=datetime.now(UTC)) is True


def test_timezone_naive_generated_at_does_not_raise():
    now = datetime(2026, 6, 1, tzinfo=UTC)
    assert _is_bundle_stale("2026-05-20T00:00:00", now=now) is False  # no offset


def test_malformed_generated_at_is_treated_as_stale():
    now = datetime(2026, 6, 1, tzinfo=UTC)
    assert _is_bundle_stale("not-a-date", now=now) is True


def test_screen_flags_stale_bundle(mock_sanctions_data):
    svc = _service(mock_sanctions_data)
    svc.bundle_generated_at = "2020-01-01T00:00:00+00:00"
    csv = b"name,dob\nClean Person,1990-01-01\n"

    result = svc.screen(csv, "list.csv")

    assert result["bundle_generated_at"] == "2020-01-01T00:00:00+00:00"
    assert result["bundle_stale"] is True


def test_screen_does_not_flag_fresh_bundle(mock_sanctions_data):
    svc = _service(mock_sanctions_data)
    svc.bundle_generated_at = datetime.now(UTC).isoformat()
    csv = b"name,dob\nClean Person,1990-01-01\n"

    result = svc.screen(csv, "list.csv")

    assert result["bundle_stale"] is False


def test_get_matcher_reads_generated_at_from_bundle_on_cold_start():
    """No pre-injected matcher (unlike every other test here, via _service()) —
    this is the real production path (backend/api/handlers.py never injects a
    matcher), so it's the only test that actually exercises the
    ``bundle.get("generated_at")`` wiring in ``_get_matcher()``."""
    s3 = FakeS3()
    bundle = {
        "generated_at": "2026-03-15T00:00:00+00:00",
        "un_list": [],
        "sema_list": [],
        "ps_list": [],
    }
    s3.put_object(
        Bucket="sanctions-bucket",
        Key="sanctions/bundle.json",
        Body=json.dumps(bundle).encode(),
    )
    svc = ScreenerService(
        s3=s3,
        outputs_bucket="outputs",
        sanctions_bucket="sanctions-bucket",
    )
    assert svc.matcher is None
    assert svc.bundle_generated_at is None

    svc._get_matcher()

    assert svc.bundle_generated_at == "2026-03-15T00:00:00+00:00"
