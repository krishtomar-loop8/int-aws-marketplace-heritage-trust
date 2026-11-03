"""Core crawl logic: enumerate granted SharePoint drives → S3 (source-agnostic).

The Graph connector and the storage layer are injected so this is fully
unit-testable without AWS or network. See :mod:`backend.lambdas.sharepoint_crawler`
for the deployed wiring.

Scale design (large legacy libraries):
- **Stream** the drive ``/delta`` one page at a time (never buffer the whole
  library in memory).
- **Checkpoint** the resume cursor after every fully-processed page, so a run
  that stops mid-enumeration resumes from the exact next page.
- **Download concurrently** within a page (a page is the unit of progress).
- **Respect a runtime budget** (the Lambda deadline): stop cleanly before the
  timeout with the cursor saved; the handler self-invokes to continue.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

from backend.search.scoping import DEFAULT_EXCLUDED_FOLDERS, is_excluded
from services.logging import get_logger

logger = get_logger(__name__)

# File types Bedrock Knowledge Bases can parse (text is extracted at ingestion).
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".pdf",
        ".doc",
        ".docx",
        ".txt",
        ".md",
        ".html",
        ".htm",
        ".csv",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
    }
)
# Bedrock KB per-file ceiling is 50 MB; stay comfortably under it.
MAX_FILE_BYTES = 45 * 1024 * 1024
# Concurrent downloads per page (read-only GETs share the connector's warmed token).
DEFAULT_WORKERS = 8


class CrawlStorage(Protocol):
    """Where crawled documents + per-site delta cursors are persisted."""

    def load_delta(self, site_key: str) -> str | None: ...
    def save_delta(self, site_key: str, delta_link: str | None) -> None: ...
    def put_document(
        self, key: str, content: bytes, *, content_type: str, metadata: dict[str, str]
    ) -> None: ...
    def delete_document(self, prefix: str) -> None: ...


@dataclass
class CrawlStats:
    sites: int = 0
    pages: int = 0
    downloaded: int = 0
    deleted: int = 0
    excluded: int = 0
    skipped_type: int = 0
    skipped_size: int = 0
    errors: int = 0
    # True if the run stopped on the runtime budget with more work pending
    # (the handler self-invokes to continue from the saved cursor).
    stopped_early: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class SharePointCrawler:
    """Copies changed SharePoint documents into the crawl bucket, incrementally.

    Each site's drive is streamed via Graph ``/delta`` from its saved cursor; the
    cursor is checkpointed after **every page** so progress survives a stop. Within
    a page, downloads run concurrently. HR/Finance content is excluded (and any
    prior copy removed); unsupported types / oversize files are skipped; a per-item
    download failure is counted, never fatal. When a ``deadline`` is given the run
    stops cleanly before it (``stopped_early=True``) with the cursor saved.
    """

    def __init__(
        self,
        *,
        graph: Any,
        storage: CrawlStorage,
        sites: list[str],
        exclude: frozenset[str] = DEFAULT_EXCLUDED_FOLDERS,
        max_bytes: int = MAX_FILE_BYTES,
        supported: frozenset[str] = SUPPORTED_EXTENSIONS,
        workers: int = DEFAULT_WORKERS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._graph = graph
        self._storage = storage
        self._sites = sites
        self._exclude = exclude
        self._max_bytes = max_bytes
        self._supported = supported
        self._workers = workers
        self._clock = clock

    def crawl(self, *, deadline: float | None = None) -> CrawlStats:
        """Crawl all sites. ``deadline`` is a monotonic timestamp to stop by
        (None = no budget). Sets ``stats.stopped_early`` if the budget cut it short.
        """
        stats = CrawlStats()
        for site_url in self._sites:
            if self._out_of_time(deadline):
                stats.stopped_early = True
                break
            try:
                self._crawl_site(site_url, stats, deadline=deadline)
            except Exception:  # noqa: BLE001 - one bad site must not sink the run
                stats.errors += 1
                logger.exception(
                    "site crawl failed", extra={"extra_fields": {"site": _site_key(site_url)}}
                )
            if stats.stopped_early:
                break
        return stats

    def _out_of_time(self, deadline: float | None) -> bool:
        return deadline is not None and self._clock() >= deadline

    def _crawl_site(
        self, site_url: str, stats: CrawlStats, *, deadline: float | None = None
    ) -> None:
        parts = urlsplit(site_url)
        site = self._graph.get_site(parts.netloc, parts.path)
        site_id = site["id"]
        site_key = _site_key(site_url)
        cursor = self._storage.load_delta(site_key)
        stats.sites += 1
        for page_items, resume_cursor in self._graph.iter_drive_delta(site_id, delta_link=cursor):
            self._process_page(site_id, site_key, page_items, stats)
            stats.pages += 1
            # Checkpoint AFTER the page is fully processed → a stop resumes from the
            # exact next page (resume_cursor is the nextLink, or the final deltaLink).
            self._storage.save_delta(site_key, resume_cursor)
            if self._out_of_time(deadline):
                stats.stopped_early = True
                return

    def _process_page(
        self, site_id: str, site_key: str, items: list[dict[str, Any]], stats: CrawlStats
    ) -> None:
        # Triage in the main thread (serial stats mutation); collect the items that
        # actually need a download, then fetch+store them concurrently.
        to_download = [it for it in items if self._triage(it, site_key, stats)]
        if not to_download:
            return
        workers = min(self._workers, len(to_download))

        def _dl(item: dict[str, Any]) -> str:
            return self._download_one(site_id, site_key, item)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(_dl, to_download))
        stats.downloaded += results.count("downloaded")
        stats.errors += results.count("error")

    def _triage(self, item: dict[str, Any], site_key: str, stats: CrawlStats) -> bool:
        """Handle non-download outcomes (serial stats mutation); return True iff the
        item should be downloaded."""
        item_id = item.get("id")
        if not item_id:
            return False
        prefix = f"{site_key}/{item_id}/"

        # Tombstone: the item was deleted/moved in SharePoint → drop our copy.
        if item.get("deleted"):
            self._storage.delete_document(prefix)
            stats.deleted += 1
            return False
        # Folders (and non-file packages) carry no body to index.
        if not item.get("file"):
            return False
        # HR/Finance is out of scope — exclude, and remove any earlier copy.
        if is_excluded(item, self._exclude):
            self._storage.delete_document(prefix)
            stats.excluded += 1
            return False
        name = item.get("name") or item_id
        ext = os.path.splitext(name)[1].lower()
        if ext not in self._supported:
            stats.skipped_type += 1
            return False
        size = item.get("size") or 0
        if size and size > self._max_bytes:
            stats.skipped_size += 1
            return False
        return True

    def _download_one(self, site_id: str, site_key: str, item: dict[str, Any]) -> str:
        """Worker (runs in a thread): download + store one item. No stats mutation —
        returns ``"downloaded"`` or ``"error"`` for the caller to tally serially."""
        item_id = item["id"]
        try:
            content = self._graph.download_drive_item(site_id, item_id)
        except Exception:  # noqa: BLE001 - a bad download is counted, not fatal
            logger.warning("download failed", extra={"extra_fields": {"item": item_id}})
            return "error"
        name = item.get("name") or item_id
        key = f"{site_key}/{item_id}/{_safe_name(name)}"
        metadata = {
            "web_url": str(item.get("webUrl") or ""),
            "name": name,
            "last_modified": str(item.get("lastModifiedDateTime") or ""),
            "site": site_key,
        }
        content_type = ((item.get("file") or {}).get("mimeType")) or "application/octet-stream"
        self._storage.put_document(key, content, content_type=content_type, metadata=metadata)
        return "downloaded"


def _site_key(site_url: str) -> str:
    """A stable, filesystem-safe key from a site URL (its last path segment)."""
    path = urlsplit(site_url).path.rstrip("/")
    seg = path.rsplit("/", 1)[-1] if path else "site"
    return _safe_name(seg) or "site"


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "._- " else "_" for c in name).strip()
