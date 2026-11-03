"""Unit tests for the AML exports service + /aml-exports API routes. Fake S3; no AWS."""

from __future__ import annotations

import io
import json
from typing import Any

from backend.api import handlers
from backend.api.aml_reports import AmlExportAccessError, AmlExportsService


def _manifest(*, estates: int, trusts: int, poa: int) -> bytes:
    return json.dumps(
        {
            "generated_at": "2026-07-01T00:00:00+00:00",
            "review_status": "pending_review",
            "estates_count": estates,
            "trusts_count": trusts,
            "poa_count": poa,
            "workbook_key": "aml/2026-07/AML_Name_Export_2026-07-01.xlsx",
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


def _service(**kw: Any) -> tuple[AmlExportsService, FakeS3]:
    objects = {
        "aml/2026-07/manifest.json": _manifest(estates=10, trusts=3, poa=2),
        "aml/2026-07/AML_Name_Export_2026-07-01.xlsx": b"XLSX",
        "aml/2026-06/manifest.json": _manifest(estates=9, trusts=3, poa=2),
    }
    s3 = FakeS3(objects, **kw)
    return AmlExportsService(s3=s3, bucket="ht-outputs"), s3


def test_list_returns_manifests_sorted_newest_month_first():
    svc, _ = _service()
    exports = svc.list()
    assert [e["period"] for e in exports] == ["2026-07", "2026-06"]
    assert exports[0]["estates_count"] == 10


def test_list_skips_unreadable_manifest():
    svc, _ = _service(fail_keys={"aml/2026-06/manifest.json"})
    exports = svc.list()
    assert [e["period"] for e in exports] == ["2026-07"]


def test_download_url_is_presigned_and_scoped():
    svc, s3 = _service()
    url = svc.download_url("aml/2026-07/AML_Name_Export_2026-07-01.xlsx")
    assert url.startswith("https://s3.example/")
    assert s3.presigned[0][2] == 300  # ExpiresIn


def test_download_url_rejects_out_of_prefix_or_traversal():
    svc, _ = _service()
    for bad in ["secrets/thing", "aml/../secrets", "quarterly/100001/x.docx", "config.json"]:
        try:
            svc.download_url(bad)
            raise AssertionError(f"expected rejection for {bad}")
        except AmlExportAccessError:
            pass


# -- API routes --------------------------------------------------------------


def _install_aml_exports(monkeypatch, aml_exports) -> None:
    deps = handlers.Deps(
        resolver=object(),
        dossier_builder=object(),
        synthesizer=object(),
        store=object(),
        self_invoke=lambda p: None,
        aml_exports=aml_exports,
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


def test_aml_exports_list_route(monkeypatch):
    svc, _ = _service()
    _install_aml_exports(monkeypatch, svc)
    resp = handlers.handler(_event("GET", "/aml-exports"))
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert len(body["exports"]) == 2


def test_aml_exports_download_route(monkeypatch):
    svc, _ = _service()
    _install_aml_exports(monkeypatch, svc)
    resp = handlers.handler(
        _event(
            "GET",
            "/aml-exports/download",
            query={"key": "aml/2026-07/AML_Name_Export_2026-07-01.xlsx"},
        )
    )
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["url"].startswith("https://s3.example/")


def test_aml_exports_download_rejects_bad_key(monkeypatch):
    svc, _ = _service()
    _install_aml_exports(monkeypatch, svc)
    resp = handlers.handler(_event("GET", "/aml-exports/download", query={"key": "secrets/x"}))
    assert resp["statusCode"] == 400


def test_aml_exports_routes_503_when_unconfigured(monkeypatch):
    _install_aml_exports(monkeypatch, None)
    assert handlers.handler(_event("GET", "/aml-exports"))["statusCode"] == 503
