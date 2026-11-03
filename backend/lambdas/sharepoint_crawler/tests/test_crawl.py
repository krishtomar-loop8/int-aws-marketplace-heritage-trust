"""Unit tests for the SharePoint crawler core. Graph + storage are faked."""

from __future__ import annotations

from typing import Any

from backend.lambdas.sharepoint_crawler.crawl import SharePointCrawler

SITE = "https://aphrodite.sharepoint.com/sites/DataA"


def _file(
    id_: str,
    name: str,
    *,
    path: str | None = None,
    web_url: str | None = None,
    size: int = 100,
    mime: str = "application/pdf",
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": id_,
        "name": name,
        "size": size,
        "file": {"mimeType": mime},
        "webUrl": web_url or f"https://x/sites/DataA/{name}",
        "lastModifiedDateTime": "2026-07-01T00:00:00Z",
    }
    if path is not None:
        item["parentReference"] = {"path": path}
    return item


class FakeGraph:
    """Streams `pages` (a list of item-lists) via iter_drive_delta.

    Yields (page_items, resume_cursor): a synthetic nextLink for non-final pages,
    `delta` on the final page — matching the real connector's contract.
    """

    def __init__(self, pages: list[list[dict[str, Any]]], *, delta: str = "DELTA-NEXT") -> None:
        self._pages = pages
        self._delta = delta
        self.downloaded: list[str] = []
        self.delta_calls: list[str | None] = []

    def get_site(self, hostname: str, path: str) -> dict[str, Any]:
        return {"id": f"site::{path}"}

    def iter_drive_delta(self, site_id: str, *, delta_link: str | None = None):
        self.delta_calls.append(delta_link)
        n = len(self._pages)
        for i, items in enumerate(self._pages):
            resume = self._delta if i == n - 1 else f"NEXT-{i + 1}"
            yield items, resume

    def download_drive_item(self, site_id: str, item_id: str) -> bytes:
        self.downloaded.append(item_id)
        return f"content of {item_id}".encode()


class FakeStorage:
    def __init__(self, deltas: dict[str, str] | None = None) -> None:
        self.docs: dict[str, tuple[bytes, dict[str, str]]] = {}
        self.deleted: list[str] = []
        self.saved: list[tuple[str, str | None]] = []  # (site_key, cursor), in order
        self._deltas = deltas or {}

    def load_delta(self, site_key: str) -> str | None:
        return self._deltas.get(site_key)

    def save_delta(self, site_key: str, delta_link: str | None) -> None:
        self.saved.append((site_key, delta_link))

    def put_document(self, key, content, *, content_type, metadata) -> None:
        self.docs[key] = (content, metadata)

    def delete_document(self, prefix: str) -> None:
        self.deleted.append(prefix)

    @property
    def cursors(self) -> list[str | None]:
        return [c for _, c in self.saved]


def _crawl(graph: FakeGraph, storage: FakeStorage, **kw):
    return SharePointCrawler(graph=graph, storage=storage, sites=[SITE], **kw)


def test_downloads_supported_files_with_metadata_sidecar() -> None:
    graph = FakeGraph([[_file("d1", "will.pdf", web_url="https://x/sites/DataA/Matters/will.pdf")]])
    storage = FakeStorage()
    stats = _crawl(graph, storage).crawl()

    assert stats.downloaded == 1
    assert graph.downloaded == ["d1"]
    (key, (content, metadata)) = next(iter(storage.docs.items()))
    assert key == "DataA/d1/will.pdf"
    assert content == b"content of d1"
    assert metadata["web_url"] == "https://x/sites/DataA/Matters/will.pdf"
    assert metadata["name"] == "will.pdf"
    assert metadata["site"] == "DataA"
    assert not stats.stopped_early


def test_excludes_hr_finance_and_removes_prior_copy() -> None:
    graph = FakeGraph(
        [
            [
                _file(
                    "d2",
                    "pay.xlsx",
                    path="/drive/root:/Finance/2026",
                    mime="application/vnd.ms-excel",
                )
            ]
        ]
    )
    storage = FakeStorage()
    stats = _crawl(graph, storage).crawl()

    assert stats.excluded == 1
    assert stats.downloaded == 0
    assert graph.downloaded == []
    assert storage.deleted == ["DataA/d2/"]  # any earlier copy is purged


def test_tombstone_deletes_document() -> None:
    graph = FakeGraph([[{"id": "d3", "deleted": {"state": "deleted"}}]])
    storage = FakeStorage()
    stats = _crawl(graph, storage).crawl()

    assert stats.deleted == 1
    assert storage.deleted == ["DataA/d3/"]


def test_skips_folders_and_unsupported_and_oversize() -> None:
    graph = FakeGraph(
        [
            [
                {"id": "f1", "name": "Matters", "folder": {"childCount": 3}},  # folder
                _file("d4", "photo.jpg", mime="image/jpeg"),  # unsupported type
                _file("d5", "huge.pdf", size=99 * 1024 * 1024),  # oversize
            ]
        ]
    )
    storage = FakeStorage()
    stats = _crawl(graph, storage).crawl()

    assert stats.downloaded == 0
    assert stats.skipped_type == 1
    assert stats.skipped_size == 1
    assert graph.downloaded == []


def test_uses_saved_delta_cursor_and_advances_it() -> None:
    graph = FakeGraph([[_file("d1", "a.pdf")]], delta="DELTA-2")
    storage = FakeStorage(deltas={"DataA": "DELTA-1"})
    _crawl(graph, storage).crawl()

    assert graph.delta_calls == ["DELTA-1"]  # resumed from the saved cursor
    assert storage.cursors == ["DELTA-2"]  # advanced to the new deltaLink


def test_download_failure_is_counted_not_fatal() -> None:
    class FlakyGraph(FakeGraph):
        def download_drive_item(self, site_id: str, item_id: str) -> bytes:
            raise RuntimeError("500 from Graph")

    graph = FlakyGraph([[_file("d1", "a.pdf"), _file("d2", "b.pdf")]])
    storage = FakeStorage()
    stats = _crawl(graph, storage).crawl()

    assert stats.errors == 2
    assert stats.downloaded == 0
    assert storage.cursors == ["DELTA-NEXT"]  # run still completes + advances cursor


def test_checkpoints_cursor_after_each_page() -> None:
    graph = FakeGraph(
        [[_file("d1", "a.pdf")], [_file("d2", "b.pdf")], [_file("d3", "c.pdf")]],
        delta="DELTA-FINAL",
    )
    storage = FakeStorage()
    stats = _crawl(graph, storage).crawl()

    assert stats.pages == 3
    assert stats.downloaded == 3
    # one checkpoint per page: nextLinks for pages 1-2, deltaLink on the final page
    assert storage.cursors == ["NEXT-1", "NEXT-2", "DELTA-FINAL"]


def test_stops_on_runtime_budget_and_saves_resume_cursor() -> None:
    # clock advances 60s each call; deadline 90 → out-of-time after the 1st page.
    calls = [0]

    def clock() -> float:
        calls[0] += 1
        return calls[0] * 60.0

    graph = FakeGraph([[_file("d1", "a.pdf")], [_file("d2", "b.pdf")]], delta="DELTA-FINAL")
    storage = FakeStorage()
    stats = _crawl(graph, storage, clock=clock).crawl(deadline=90.0)

    assert stats.stopped_early is True
    assert stats.pages == 1  # stopped after the first page
    assert graph.downloaded == ["d1"]  # second page never fetched
    assert storage.cursors == ["NEXT-1"]  # resume point saved (not the deltaLink)


def test_concurrent_downloads_store_all_items() -> None:
    page = [_file(f"d{i}", f"f{i}.pdf") for i in range(6)]
    graph = FakeGraph([page])
    storage = FakeStorage()
    stats = _crawl(graph, storage, workers=4).crawl()

    assert stats.downloaded == 6
    assert set(graph.downloaded) == {f"d{i}" for i in range(6)}
    assert len(storage.docs) == 6


def test_not_stopped_early_when_complete() -> None:
    graph = FakeGraph([[_file("d1", "a.pdf")]])
    stats = _crawl(graph, FakeStorage()).crawl(deadline=None)
    assert stats.stopped_early is False
