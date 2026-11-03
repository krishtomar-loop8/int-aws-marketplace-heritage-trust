"""S3-backed :class:`~backend.lambdas.sharepoint_crawler.crawl.CrawlStorage`.

Layout in the crawl bucket:
- ``documents/<site>/<item-id>/<name>``            — the document (KB data source)
- ``documents/<site>/<item-id>/<name>.metadata.json`` — Bedrock KB metadata sidecar
- ``_state/<site>.deltalink``                       — the per-site Graph delta cursor

The ``.metadata.json`` sidecars carry the SharePoint deep-link + name + date so
they come back in ``Retrieve`` results (see ``services.knowledge_base``). Objects
are SSE-KMS encrypted when a key is configured.
"""

from __future__ import annotations

import json
from typing import Any


class S3CrawlStorage:
    def __init__(
        self,
        s3: Any,
        bucket: str,
        *,
        prefix: str = "documents",
        state_prefix: str = "_state",
        kms_key_id: str | None = None,
    ) -> None:
        self._s3 = s3
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._state_prefix = state_prefix.strip("/")
        self._kms_key_id = kms_key_id

    # -- documents ----------------------------------------------------------

    def put_document(
        self, key: str, content: bytes, *, content_type: str, metadata: dict[str, str]
    ) -> None:
        full = f"{self._prefix}/{key}"
        self._put(full, content, content_type)
        sidecar = json.dumps({"metadataAttributes": metadata}).encode("utf-8")
        self._put(f"{full}.metadata.json", sidecar, "application/json")

    def delete_document(self, prefix: str) -> None:
        full = f"{self._prefix}/{prefix}"
        keys = self._list_keys(full)
        for k in keys:
            self._s3.delete_object(Bucket=self._bucket, Key=k)

    # -- delta cursor -------------------------------------------------------

    def load_delta(self, site_key: str) -> str | None:
        key = f"{self._state_prefix}/{site_key}.deltalink"
        try:
            obj = self._s3.get_object(Bucket=self._bucket, Key=key)
        except Exception:  # noqa: BLE001 - absent cursor ⇒ full crawl
            return None
        body = obj["Body"].read()
        text = body.decode("utf-8") if isinstance(body, bytes) else str(body)
        return text.strip() or None

    def save_delta(self, site_key: str, delta_link: str | None) -> None:
        if not delta_link:
            return
        key = f"{self._state_prefix}/{site_key}.deltalink"
        self._put(key, delta_link.encode("utf-8"), "text/plain")

    # -- helpers ------------------------------------------------------------

    def _put(self, key: str, body: bytes, content_type: str) -> None:
        args: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": key,
            "Body": body,
            "ContentType": content_type,
        }
        if self._kms_key_id:
            args["ServerSideEncryption"] = "aws:kms"
            args["SSEKMSKeyId"] = self._kms_key_id
        self._s3.put_object(**args)

    def _list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        token: str | None = None
        while True:
            kw: dict[str, Any] = {"Bucket": self._bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            resp = self._s3.list_objects_v2(**kw)
            keys.extend(o["Key"] for o in resp.get("Contents", []))
            if not resp.get("IsTruncated"):
                return keys
            token = resp.get("NextContinuationToken")
