"""Content scoping — the one access rule for the matter-search agent.

Aphrodite confirmed *"everyone has access to everything"* except the
**HR and Finance folders in SharePoint**. There is no per-user/matter authz;
the only restriction is to drop SharePoint documents that live under an excluded
top-level area (HR / Finance) before they reach the dossier or the model.

This works on Microsoft Graph ``driveItem``-shaped results (a search hit's
``resource``, or a drive-children item): it inspects the item's folder path
(``parentReference.path`` and ``webUrl``) and excludes the item if any path
**segment** matches an excluded name. Segment matching (not substring) avoids
false positives like "Financial Planning" or "HR-Advisory Client".
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urlsplit

# Folder names to exclude, case-insensitive (matched against whole path segments).
DEFAULT_EXCLUDED_FOLDERS: frozenset[str] = frozenset({"hr", "human resources", "finance"})

# A SharePoint drive path looks like "/drives/{id}/root:/Finance/2024/x.docx" or
# a parentReference.path like "/drive/root:/HR/Payroll"; the meaningful part is
# after "root:". webUrl paths are plain "/sites/Team/Shared Documents/HR/...".
_ROOT_MARKER = re.compile(r"root:?/?", re.IGNORECASE)


def _path_segments(item: dict[str, Any]) -> set[str]:
    """Lower-cased folder segments from an item's parentReference path + webUrl."""
    segments: set[str] = set()

    parent = item.get("parentReference") or {}
    raw_path = parent.get("path")
    if isinstance(raw_path, str) and raw_path:
        after_root = _ROOT_MARKER.split(raw_path, maxsplit=1)
        tail = after_root[-1] if len(after_root) > 1 else raw_path
        segments.update(_split(tail))

    web_url = item.get("webUrl")
    if isinstance(web_url, str) and web_url:
        segments.update(_split(urlsplit(web_url).path))

    return segments


def _split(path: str) -> set[str]:
    return {unquote(seg).strip().lower() for seg in path.split("/") if seg.strip()}


def is_excluded(item: dict[str, Any], exclude: frozenset[str] = DEFAULT_EXCLUDED_FOLDERS) -> bool:
    """True if the document sits under an excluded (HR/Finance) folder."""
    return bool(_path_segments(item) & exclude)


def belongs_to_matter(
    web_url: str | None,
    name: str | None,
    text: str | None,
    *,
    matter_numbers: frozenset[str],
    tokens: frozenset[str],
) -> bool:
    """True if a retrieved SharePoint hit plausibly belongs to the searched matter.

    The KB is a single firm-wide index with no matter metadata, so an unfiltered
    semantic search pulls in other clients' documents — a confidentiality problem.
    Until the index carries a filterable matter id (a re-crawl), scope each hit by
    the identifiers we can derive from the resolved matter:

    - the 6-digit **matter number** appearing as a whole token in the document's
      URL path (``…/Shared Documents/C/<client>/<matter>/…``) or filename, or
    - **client-name tokens** appearing as whole words in the filename, URL, or
      the retrieved text chunk — at least **TWO distinct** tokens. A single
      shared surname ("bell", "desmond") is how other clients' documents leaked
      into matter dossiers, so one token is **never** enough: a matter whose
      usable name reduces to a single token is scoped by its matter number
      alone, and its SharePoint section is left empty rather than filled with
      every namesake's file.

    Word-boundary matching (not substring) so "bell" cannot match "Campbell".
    Fails **closed** when no scope could be derived — an unrelated firm document
    is worse than an empty SharePoint section (the caller reports the skip).
    """
    if not matter_numbers and not tokens:
        return False
    url = (web_url or "").lower()
    name_l = (name or "").lower()
    for num in matter_numbers:
        pat = rf"\b{re.escape(num)}\b"
        if re.search(pat, url) or re.search(pat, name_l):
            return True
    # A single client-name token is not sufficient — one shared surname admitted
    # every namesake's document (a "bell" scope pulled in Bruce/Peter/Lawrence Bell
    # and Desmond Power files). Require two distinct tokens to co-occur; fewer than
    # two means name-scoping is unsafe, so fall back to the matter number alone.
    if len(tokens) < 2:
        return False
    haystack = f"{name_l} {(text or '').lower()} {url}"
    hits = sum(1 for tok in tokens if re.search(rf"\b{re.escape(tok)}\b", haystack))
    return hits >= 2


def filter_documents(
    items: list[dict[str, Any]], *, exclude: frozenset[str] = DEFAULT_EXCLUDED_FOLDERS
) -> tuple[list[dict[str, Any]], int]:
    """Return ``(kept, excluded_count)`` — drops HR/Finance documents.

    ``excluded_count`` is surfaced so the caller can log/annotate that some
    content was withheld (rather than silently dropping it).
    """
    kept = [it for it in items if not is_excluded(it, exclude)]
    return kept, len(items) - len(kept)
