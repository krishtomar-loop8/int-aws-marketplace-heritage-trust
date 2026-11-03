"""Unit tests for the reports service + /reports API routes. Fake S3; no AWS."""

from __future__ import annotations

import io
import json
from typing import Any

from backend.api import handlers
from backend.api.reports import ReportAccessError, ReportsService


def _manifest(matter: str, period: str) -> bytes:
    return json.dumps(
        {
            "matter_number": matter,
            "period": period,
            "trust_name": f"{matter} Trust",
            "review_status": "pending_review",
            "investment_pending": True,
        }
    ).encode()


class FakeS3:
    def __init__(self, objects: dict[str, bytes], *, fail_keys: set[str] | None = None) -> None:
        self._objects = objects
        self._fail = fail_keys or set()
        self.presigned: list[tuple[str, dict, int]] = []

    def get_paginator(self, name: str):
        assert name == "list_objects_v2"
        objects = self._objects

        class _Paginator:
            def paginate(self, Bucket, Prefix):  # noqa: N803 - boto3 kwarg names
                yield {"Contents": [{"Key": k} for k in objects if k.startswith(Prefix)]}

        return _Paginator()

    def get_object(self, Bucket, Key):  # noqa: N803
        if Key in self._fail:
            raise RuntimeError("boom")
        return {"Body": io.BytesIO(self._objects[Key])}

    def generate_presigned_url(self, op, Params, ExpiresIn):  # noqa: N803
        self.presigned.append((op, Params, ExpiresIn))
        return f"https://s3.example/{Params['Key']}?sig=1"


def _service(**kw: Any) -> tuple[ReportsService, FakeS3]:
    objects = {
        "quarterly/100001/2026-Q1/manifest.json": _manifest("100001", "2026-Q1"),
        "quarterly/100001/2026-Q1/cover-letter.docx": b"DOCX",
        "quarterly/100001/2026-Q1/trust-ledger.pdf": b"PDF",
        "quarterly/100002/2025-Q4/manifest.json": _manifest("100002", "2025-Q4"),
    }
    s3 = FakeS3(objects, **kw)
    return ReportsService(s3=s3, bucket="ht-outputs"), s3


def test_list_returns_manifests_sorted_newest_first():
    svc, _ = _service()
    reports = svc.list()
    assert [(r["matter_number"], r["period"]) for r in reports] == [
        ("100002", "2025-Q4"),
        ("100001", "2026-Q1"),
    ]
    assert reports[0]["trust_name"] == "100002 Trust"


def test_list_skips_unreadable_manifest():
    svc, _ = _service(fail_keys={"quarterly/100002/2025-Q4/manifest.json"})
    reports = svc.list()
    assert [r["matter_number"] for r in reports] == ["100001"]


def test_download_url_is_presigned_and_scoped():
    svc, s3 = _service()
    url = svc.download_url("quarterly/100001/2026-Q1/cover-letter.docx")
    assert url.startswith("https://s3.example/")
    assert s3.presigned[0][2] == 300  # ExpiresIn


def test_download_url_rejects_out_of_prefix_or_traversal():
    svc, _ = _service()
    for bad in ["secrets/thing", "quarterly/../secrets", "config.json"]:
        try:
            svc.download_url(bad)
            raise AssertionError(f"expected rejection for {bad}")
        except ReportAccessError:
            pass


# -- API routes --------------------------------------------------------------


def _install_reports(monkeypatch, reports) -> None:
    deps = handlers.Deps(
        resolver=object(),
        dossier_builder=object(),
        synthesizer=object(),
        store=object(),
        self_invoke=lambda p: None,
        reports=reports,
    )
    monkeypatch.setattr(handlers, "_DEPS", deps)


def _event(method: str, path: str, *, query: dict | None = None) -> dict:
    return {
        "requestContext": {
            "http": {"method": method, "path": path},
            "authorizer": {"jwt": {"claims": {"sub": "u1", "email": "a@b.ca"}}},
        },
        "queryStringParameters": query,
    }


def test_reports_list_route(monkeypatch):
    svc, _ = _service()
    _install_reports(monkeypatch, svc)
    resp = handlers.handler(_event("GET", "/reports"))
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert len(body["reports"]) == 2


def test_reports_download_route(monkeypatch):
    svc, _ = _service()
    _install_reports(monkeypatch, svc)
    resp = handlers.handler(
        _event(
            "GET", "/reports/download", query={"key": "quarterly/100001/2026-Q1/trust-ledger.pdf"}
        )
    )
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["url"].startswith("https://s3.example/")


def test_reports_download_rejects_bad_key(monkeypatch):
    svc, _ = _service()
    _install_reports(monkeypatch, svc)
    resp = handlers.handler(_event("GET", "/reports/download", query={"key": "secrets/x"}))
    assert resp["statusCode"] == 400


def test_reports_routes_503_when_unconfigured(monkeypatch):
    _install_reports(monkeypatch, None)
    assert handlers.handler(_event("GET", "/reports"))["statusCode"] == 503
