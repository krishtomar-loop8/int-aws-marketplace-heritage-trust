"""Unit tests for the S3 crawl storage. A fake S3 client records calls."""

from __future__ import annotations

import json
from typing import Any

from backend.lambdas.sharepoint_crawler.storage import S3CrawlStorage


class FakeS3:
    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self.objects: dict[str, bytes] = objects or {}
        self.puts: list[dict[str, Any]] = []
        self.deleted: list[str] = []

    def put_object(self, **kw: Any) -> None:
        self.puts.append(kw)
        self.objects[kw["Key"]] = kw["Body"]

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        if Key not in self.objects:
            raise RuntimeError("NoSuchKey")
        body = self.objects[Key]

        class _Body:
            def read(self_inner) -> bytes:
                return body

        return {"Body": _Body()}

    def list_objects_v2(self, **kw: Any) -> dict[str, Any]:
        prefix = kw["Prefix"]
        contents = [{"Key": k} for k in self.objects if k.startswith(prefix)]
        return {"Contents": contents, "IsTruncated": False}

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        self.deleted.append(Key)
        self.objects.pop(Key, None)


def test_put_document_writes_file_and_metadata_sidecar() -> None:
    s3 = FakeS3()
    store = S3CrawlStorage(s3, "ht-crawl", kms_key_id="kms-1")
    store.put_document(
        "DataA/d1/will.pdf",
        b"PDFDATA",
        content_type="application/pdf",
        metadata={"web_url": "https://x/will.pdf", "name": "will.pdf"},
    )

    assert s3.objects["documents/DataA/d1/will.pdf"] == b"PDFDATA"
    sidecar = json.loads(s3.objects["documents/DataA/d1/will.pdf.metadata.json"])
    assert sidecar == {"metadataAttributes": {"web_url": "https://x/will.pdf", "name": "will.pdf"}}
    # SSE-KMS applied when a key is configured
    assert all(p.get("ServerSideEncryption") == "aws:kms" for p in s3.puts)
    assert all(p.get("SSEKMSKeyId") == "kms-1" for p in s3.puts)


def test_delete_document_removes_all_objects_under_prefix() -> None:
    s3 = FakeS3(
        {
            "documents/DataA/d1/will.pdf": b"x",
            "documents/DataA/d1/will.pdf.metadata.json": b"{}",
            "documents/DataA/d2/other.pdf": b"y",
        }
    )
    store = S3CrawlStorage(s3, "ht-crawl")
    store.delete_document("DataA/d1/")

    assert "documents/DataA/d1/will.pdf" not in s3.objects
    assert "documents/DataA/d1/will.pdf.metadata.json" not in s3.objects
    assert "documents/DataA/d2/other.pdf" in s3.objects  # a different item is untouched


def test_delta_cursor_roundtrip_and_missing_is_none() -> None:
    s3 = FakeS3()
    store = S3CrawlStorage(s3, "ht-crawl")
    assert store.load_delta("DataA") is None  # no cursor yet ⇒ full crawl

    store.save_delta("DataA", "https://graph/delta?token=abc")
    assert store.load_delta("DataA") == "https://graph/delta?token=abc"
    assert "_state/DataA.deltalink" in s3.objects


def test_save_delta_noops_on_empty() -> None:
    s3 = FakeS3()
    S3CrawlStorage(s3, "ht-crawl").save_delta("DataA", None)
    assert s3.puts == []
