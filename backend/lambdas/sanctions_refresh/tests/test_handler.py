"""Tests for the sanctions-refresh Lambda handler (fake S3, patched fetcher)."""

import json

import pytest

from backend.lambdas.sanctions_refresh import handler as h
from backend.screener import fetcher


class FakeS3:
    def __init__(self) -> None:
        self.puts: dict[str, bytes] = {}

    def put_object(self, Bucket, Key, Body, **kwargs):  # noqa: N803 - boto3 kwarg names
        self.puts[Key] = Body


def _inject(monkeypatch, s3, data, notify_failure=None):
    monkeypatch.setattr(fetcher, "fetch_all", lambda force_http=False: data)
    deps = h.Deps(
        s3=s3,
        bucket="ref",
        key="sanctions/bundle.json",
        kms_key_id=None,
        notify_failure=notify_failure or (lambda *a: None),
    )
    monkeypatch.setattr(h, "_DEPS", deps)


def test_refresh_writes_bundle(monkeypatch):
    s3 = FakeS3()
    _inject(
        monkeypatch,
        s3,
        {
            "un_list": [{"full_name": "X"}],
            "sema_list": [{"full_name": "Y"}, {"full_name": "Y2"}],
            "ps_list": [{"full_name": "Z"}],
        },
    )

    out = h.handler({}, None)

    assert out["counts"]["total"] == 4
    body = json.loads(s3.puts["sanctions/bundle.json"])
    assert body["counts"] == {"un": 1, "sema": 2, "ps": 1, "total": 4}
    assert "generated_at" in body


def test_refresh_aborts_on_partial_fetch(monkeypatch):
    s3 = FakeS3()
    _inject(
        monkeypatch,
        s3,
        {"un_list": [], "sema_list": [{"full_name": "Y"}], "ps_list": [{"full_name": "Z"}]},
    )

    with pytest.raises(RuntimeError):
        h.handler({}, None)
    assert s3.puts == {}  # good bundle not clobbered


def test_refresh_notifies_on_partial_fetch(monkeypatch):
    s3 = FakeS3()
    failures = []
    _inject(
        monkeypatch,
        s3,
        {"un_list": [], "sema_list": [{"full_name": "Y"}], "ps_list": [{"full_name": "Z"}]},
        notify_failure=lambda error, counts: failures.append((error, counts)),
    )

    with pytest.raises(RuntimeError):
        h.handler({}, None)

    assert len(failures) == 1
    error, counts = failures[0]
    assert counts == {"un": 0, "sema": 1, "ps": 1, "total": 2}
    assert "incomplete" in error


def test_refresh_notifies_on_s3_write_failure_after_full_fetch(monkeypatch):
    class FailingS3(FakeS3):
        def put_object(self, **kwargs):
            raise RuntimeError("S3 unavailable")

    s3 = FailingS3()
    failures = []
    _inject(
        monkeypatch,
        s3,
        {
            "un_list": [{"full_name": "X"}],
            "sema_list": [{"full_name": "Y"}],
            "ps_list": [{"full_name": "Z"}],
        },
        notify_failure=lambda error, counts: failures.append((error, counts)),
    )

    with pytest.raises(RuntimeError, match="S3 unavailable"):
        h.handler({}, None)

    assert len(failures) == 1
    assert failures[0][1] == {"un": 1, "sema": 1, "ps": 1, "total": 3}  # full counts were known


def test_notify_failure_raising_does_not_mask_the_original_exception(monkeypatch):
    s3 = FakeS3()
    _inject(
        monkeypatch,
        s3,
        {"un_list": [], "sema_list": [], "ps_list": []},
        notify_failure=lambda *a: (_ for _ in ()).throw(RuntimeError("sns down")),
    )

    with pytest.raises(RuntimeError, match="incomplete"):
        h.handler({}, None)
