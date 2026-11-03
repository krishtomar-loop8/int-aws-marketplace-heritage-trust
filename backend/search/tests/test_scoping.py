"""Unit tests for the SharePoint HR/Finance exclusion filter."""

from __future__ import annotations

from typing import Any

from backend.search.scoping import belongs_to_matter, filter_documents, is_excluded


def item(
    *, path: str | None = None, web_url: str | None = None, name: str = "doc.docx"
) -> dict[str, Any]:
    it: dict[str, Any] = {"name": name}
    if path is not None:
        it["parentReference"] = {"path": path}
    if web_url is not None:
        it["webUrl"] = web_url
    return it


# -- exclusion by folder segment --------------------------------------------


def test_excludes_finance_via_parent_path() -> None:
    assert is_excluded(item(path="/drives/b!x/root:/Finance/2024"))


def test_excludes_hr_via_parent_path() -> None:
    assert is_excluded(item(path="/drive/root:/HR/Payroll"))


def test_excludes_via_weburl() -> None:
    assert is_excluded(
        item(web_url="https://ht.sharepoint.com/sites/Team/Shared%20Documents/HR/x.docx")
    )


def test_excludes_human_resources_multiword() -> None:
    assert is_excluded(item(path="/drive/root:/Human Resources/policies"))


def test_case_insensitive() -> None:
    assert is_excluded(item(path="/drive/root:/fInAnCe/q1"))


# -- no false positives ------------------------------------------------------


def test_keeps_matter_documents() -> None:
    assert not is_excluded(item(path="/drive/root:/Matters/Smith Estate/KYC"))


def test_segment_match_not_substring() -> None:
    # "Financial Planning" / "HR-Advisory" contain the letters but aren't the
    # excluded HR/Finance folders.
    assert not is_excluded(item(path="/drive/root:/Financial Planning/notes"))
    assert not is_excluded(item(path="/drive/root:/HR-Advisory Client/file"))


def test_item_without_path_is_kept() -> None:
    assert not is_excluded({"name": "orphan.docx"})


# -- filter_documents --------------------------------------------------------


def test_filter_documents_partitions_and_counts() -> None:
    items = [
        item(path="/drive/root:/Matters/Lee/a.docx", name="a"),
        item(path="/drive/root:/Finance/b.docx", name="b"),
        item(web_url="https://ht.sharepoint.com/sites/T/Docs/HR/c.docx", name="c"),
        item(path="/drive/root:/Matters/Lee/d.docx", name="d"),
    ]
    kept, excluded = filter_documents(items)
    assert [i["name"] for i in kept] == ["a", "d"]
    assert excluded == 2


def test_custom_exclude_set() -> None:
    items = [item(path="/drive/root:/Legal/x"), item(path="/drive/root:/Payroll/y")]
    kept, excluded = filter_documents(items, exclude=frozenset({"payroll"}))
    assert excluded == 1
    assert kept[0]["parentReference"]["path"].endswith("/Legal/x")


# -- matter scoping (belongs_to_matter) -------------------------------------

NUMS = frozenset({"250028"})
TOKENS = frozenset({"trethewey", "vista"})


def test_belongs_by_matter_number_in_url_path() -> None:
    # Path-style URL carrying the matter number → in scope even with a generic name.
    url = "https://x/sites/ExampleSite/Shared%20Documents/C/2485/250028/DOC/questionnaire.pdf"
    assert belongs_to_matter(
        url, "questionnaire.pdf", "Client questionnaire.", matter_numbers=NUMS, tokens=TOKENS
    )


def test_belongs_by_client_name_in_filename() -> None:
    # Viewer-style URL with no folder path, but the filename names the client.
    url = "https://x/sites/ExampleSite/_layouts/15/Doc.aspx?file=Will-Trethewey.DOC"
    assert belongs_to_matter(
        url, "Will - Vista Trethewey.DOC", "A will.", matter_numbers=NUMS, tokens=TOKENS
    )


def test_belongs_by_client_name_in_text() -> None:
    # Both name tokens present across name+text → in scope.
    assert belongs_to_matter(
        None,
        "deed.pdf",
        "Deed of transfer for the Vista Trethewey estate.",
        matter_numbers=NUMS,
        tokens=TOKENS,
    )


def test_single_shared_token_is_rejected() -> None:
    # One shared fragment of a multi-token client name is NOT enough — this is
    # how "Desmond Power" / "Bruce Bell" documents leaked into other matters.
    assert not belongs_to_matter(
        None,
        "deed.pdf",
        "Deed of transfer for the Trethewey estate.",  # only 1 of {trethewey, vista}
        matter_numbers=NUMS,
        tokens=TOKENS,
    )


def test_single_token_scope_fails_closed() -> None:
    # A one-token scope is unsafe: a lone surname admits every namesake's file
    # (a "bell" scope pulled in Bruce/Peter/Lawrence Bell and Desmond Power docs).
    # With no matter number to lean on, the document is left OUT.
    assert not belongs_to_matter(
        None,
        "Will - Trethewey.DOC",
        "A will.",
        matter_numbers=frozenset(),
        tokens=frozenset({"trethewey"}),
    )


def test_single_token_scope_still_admits_by_matter_number() -> None:
    # ...but a one-token matter is still scoped by its matter number in the path.
    url = "https://x/sites/ExampleSite/Shared%20Documents/C/2485/250028/DOC/x.pdf"
    assert belongs_to_matter(
        url, "x.pdf", "A will.", matter_numbers=NUMS, tokens=frozenset({"trethewey"})
    )


def test_other_client_namesake_document_is_rejected() -> None:
    # Regression for the Bell leak: a "Desmond Bell" scope must not admit another
    # client's document that merely shares ONE of the tokens (a Bruce Bell will,
    # a Desmond Power will). Only one of {desmond, bell} appears in each → out.
    bell_tokens = frozenset({"desmond", "bell", "radloff"})
    assert not belongs_to_matter(
        None, "2018-10-26 Bruce Quentin Bell Will.DOCX", "Will of Bruce Quentin Bell.",
        matter_numbers=frozenset(), tokens=bell_tokens,
    )
    assert not belongs_to_matter(
        None, "Desmond Anthony Power Will.DOC", "Will of Desmond Anthony Power.",
        matter_numbers=frozenset(), tokens=bell_tokens,
    )


def test_token_match_is_word_boundary_not_substring() -> None:
    # "bell" must not match "Campbell" / "Bellevue".
    assert not belongs_to_matter(
        None,
        "Campbell holdings.pdf",
        "Bellevue property statement for Campbell.",
        matter_numbers=frozenset(),
        tokens=frozenset({"bell", "desmond"}),
    )


def test_matter_number_in_document_name_passes() -> None:
    # Docs named only by matter number (no client name) stay in scope.
    assert belongs_to_matter(
        "https://x/_layouts/15/Doc.aspx?sourcedoc=abc",
        "250028 - correspondence.pdf",
        "Letter.",
        matter_numbers=NUMS,
        tokens=TOKENS,
    )


def test_off_matter_document_is_rejected() -> None:
    # Another client's document: different matter number, different name, no token.
    url = "https://x/sites/ExampleSite/Shared%20Documents/C/9999/240099/DOC/Jones-deed.pdf"
    assert not belongs_to_matter(
        url,
        "Jones-deed.pdf",
        "Trust deed for the Jones family.",
        matter_numbers=NUMS,
        tokens=TOKENS,
    )


def test_matter_number_not_matched_in_text_only() -> None:
    # A 6-digit number appearing in a doc's TEXT (e.g. an amount) must NOT scope it
    # in — the number is matched only in the URL path.
    assert not belongs_to_matter(
        None, "invoice.pdf", "Total 250028 dollars.", matter_numbers=NUMS, tokens=frozenset()
    )


def test_fails_closed_when_no_scope_derived() -> None:
    # With no scope keys we cannot tell whose document this is — keep it OUT of
    # the dossier (the orchestrator skips the pull and reports the skip instead).
    assert not belongs_to_matter(
        None, "anything.pdf", "x", matter_numbers=frozenset(), tokens=frozenset()
    )
