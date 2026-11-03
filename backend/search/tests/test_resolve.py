"""Unit tests for matter resolution. Connectors are faked; no AWS/network."""

from __future__ import annotations

from typing import Any

from backend.search.resolve import MatterResolver, _tidy_name, extract_term


class FakeEstateably:
    def __init__(self, hits: list[dict[str, Any]] | Exception) -> None:
        self._hits = hits

    def global_search(self, term: str, **_: Any) -> list[dict[str, Any]]:
        if isinstance(self._hits, Exception):
            raise self._hits
        return self._hits


class FakeClio:
    def __init__(self, hits: list[dict[str, Any]] | Exception) -> None:
        self._hits = hits

    def search_matters(self, term: str, **_: Any) -> list[dict[str, Any]]:
        if isinstance(self._hits, Exception):
            raise self._hits
        return self._hits


def est(id_: str, title: str) -> dict[str, Any]:
    return {"id": id_, "type": "estate", "attributes": {"title": title}}


def clio(
    id_: Any, description: str, display_number: str = "", client_id: Any = None
) -> dict[str, Any]:
    h: dict[str, Any] = {"id": id_, "description": description, "display_number": display_number}
    if client_id is not None:
        h["client"] = {"id": client_id, "name": "Client"}
    return h


def gs_hit(
    case_id: str, case_name: str, *, entity_id: str | None = None, value: str | None = None
) -> dict[str, Any]:
    """An Estateably globalSearch hit. The *case* is in fields.caseId/caseName;
    ``entityId`` is the matched entity (the contact id for a contact hit)."""
    return {
        "id": f"hit-{entity_id or case_id}",
        "attributes": {
            "entityId": entity_id or case_id,
            "value": value or case_name,
            "fields": {"caseId": case_id, "caseName": case_name},
        },
    }


# -- term extraction --------------------------------------------------------


def test_extract_term_strips_boilerplate_and_detects_estate() -> None:
    term, mtype = extract_term("What's the status on the Smith estate?")
    assert term == "smith"
    assert mtype == "estate"


def test_extract_term_detects_trust() -> None:
    term, mtype = extract_term("Give me an update on the Brown Trust")
    assert term == "brown"
    assert mtype == "trust"


def test_extract_term_multiword_name_drops_initials() -> None:
    # Single-letter filler (e.g. a middle initial) is dropped — fine for search.
    term, _ = extract_term("status of the John A Smith estate")
    assert term == "john smith"


# -- correlation across sources --------------------------------------------


def test_resolves_single_matter_across_both_systems() -> None:
    resolver = MatterResolver(
        estateably=FakeEstateably([est("E1", "Estate of John Smith")]),
        clio=FakeClio([clio(123, "Smith Estate administration", "00042-Smith")]),
    )
    result = resolver.resolve("What's the status on the Smith estate?")

    assert result.term == "smith"
    cand = result.resolved
    assert cand is not None  # single unambiguous candidate
    assert cand.systems == {"estateably", "clio"}
    assert cand.matter_type == "estate"
    assert {r.id for r in cand.refs} == {"E1", "123"}


def test_clio_ref_carries_client_contact_id_for_quickbooks() -> None:
    # Clio's QuickBooks Online sync stamps the matter's CLIENT-CONTACT id (not the
    # matter id) into the QBO customer DisplayName, so the Clio ref must carry it for
    # the QuickBooks reconciliation to find the customer.
    resolver = MatterResolver(
        clio=FakeClio(
            [clio(1799823657, "Estate of Verner Elwood Kober", "250005", client_id=2369563557)]
        ),
    )
    cand = resolver.resolve("status of the Verner Elwood Kober estate").resolved
    assert cand is not None
    clio_ref = next(r for r in cand.refs if r.system == "clio")
    assert clio_ref.id == "1799823657"  # matter id drives the Clio API fan-out
    assert clio_ref.qb_customer_id == "2369563557"  # client-contact id for the QB match


def test_scope_clio_skips_estateably_search() -> None:
    # Cam's case: an old matter that lives in Clio (+ SharePoint) with no Estateably
    # record. scope="clio" must skip the Estateably search so the Clio matter surfaces
    # instead of being outranked/dropped by an Estateably-corroborated current matter.
    resolver = MatterResolver(
        estateably=FakeEstateably([est("E1", "Kober Trust")]),
        clio=FakeClio([clio(111, "Estate of Verner Kober", "250005", client_id=222)]),
    )

    # Default scope searches both sources.
    default = resolver.resolve("Kober")
    assert "estateably" in {r.system for c in default.candidates for r in c.refs}

    # scope="clio" ignores Estateably entirely; the Clio matter is returned.
    scoped = resolver.resolve("Kober", scope="clio")
    systems = {r.system for c in scoped.candidates for r in c.refs}
    assert systems == {"clio"}
    assert any(r.id == "111" for c in scoped.candidates for r in c.refs)


def test_multiple_partial_matches_need_disambiguation() -> None:
    # "John Lee" partially matches two distinct matters (one via 'john', one via
    # 'lee') that share no tokens with each other → genuine disambiguation.
    resolver = MatterResolver(
        estateably=FakeEstateably([est("E1", "John Smith Estate")]),
        clio=FakeClio([clio(9, "Robert Lee Estate")]),
    )
    result = resolver.resolve("John Lee")

    assert result.resolved is None  # ambiguous — both score > 0, don't merge
    assert len(result.candidates) == 2


def test_zero_overlap_candidate_dropped_so_clear_match_resolves() -> None:
    # McDonald case: Clio returns the estate (name match) + a same-family
    # sub-matter whose label lacks the client name (score 0) → auto-resolve.
    resolver = MatterResolver(
        clio=FakeClio(
            [
                clio(1, "Estate of Oonagh McDonald", "220018"),
                clio(2, "Tax holdback. Interim distribution", "220018-01"),
            ]
        ),
    )
    result = resolver.resolve("What's the status on the Oonagh McDonald estate?")
    assert result.resolved is not None
    assert result.resolved.name == "Estate of Oonagh McDonald"


def test_estateably_globalsearch_shape_merges_with_clio() -> None:
    # Estateably globalSearch hit: id in attributes.entityId, name in value.
    est_hit = {
        "id": "hit-1",
        "attributes": {
            "type": "File Name",
            "entityId": "CASE9",
            "value": "Oonagh McDonald - 220018",
            "fields": {"caseId": "CASE9", "caseName": "Oonagh McDonald - 220018"},
        },
    }
    resolver = MatterResolver(
        estateably=FakeEstateably([est_hit]),
        clio=FakeClio([clio(7, "Estate of Oonagh Geraldine McDonald")]),
    )
    cand = resolver.resolve("Oonagh McDonald estate").resolved
    assert cand is not None
    assert cand.systems == {"clio", "estateably"}
    est_ref = next(r for r in cand.refs if r.system == "estateably")
    assert est_ref.id == "CASE9"  # entityId, not the hit id


def test_estateably_hit_resolves_to_case_not_matched_entity() -> None:
    # A contact hit (Ben Yurkiw) carries the *case* in fields.caseId/caseName. The
    # ref must be the case (YURK) with the case name, NOT the contact's entityId —
    # keying on entityId resolves to the wrong estate (whose summary then 402/404s).
    hits = [gs_hit("YURK", "David Yurkiw - 190024", entity_id="ben-contact", value="Ben Yurkiw")]
    cand = MatterResolver(estateably=FakeEstateably(hits)).resolve("David Yurkiw").resolved
    assert cand is not None
    est = [r for r in cand.refs if r.system == "estateably"]
    assert [r.id for r in est] == ["YURK"]  # caseId, not the contact entityId
    assert est[0].label == "David Yurkiw - 190024"  # caseName, not the contact value


def test_common_first_name_does_not_over_merge_distinct_cases() -> None:
    # globalSearch for a "David ..." name returns many unrelated "David" cases plus
    # a contact inside the target case. They must not collapse into one candidate
    # via the shared first name; the full-name query resolves to the right estate.
    hits = [
        gs_hit("YURK", "David Yurkiw - 190024"),
        gs_hit("YURK", "David Yurkiw - 190024", entity_id="benY", value="Ben Yurkiw"),
        gs_hit("MALEK", "David Malek - 230021"),
        gs_hit("SPARKS", "David Sparks - 250006"),
        gs_hit("HOWDEN", "John David Howden - 240003"),
        gs_hit("JACK", "David Jackman - 260020"),
    ]
    resolver = MatterResolver(
        estateably=FakeEstateably(hits),
        clio=FakeClio([clio(190024, "Administration of the Estate of David Thomas George Yurkiw")]),
    )
    result = resolver.resolve("David Thomas George Yurkiw")

    cand = result.resolved
    assert cand is not None  # the clear leader auto-resolves, no disambiguation on siblings
    est = [r for r in cand.refs if r.system == "estateably"]
    assert [r.id for r in est] == ["YURK"]  # right case, deduped, no "David" siblings
    assert cand.systems == {"estateably", "clio"}


def test_distinct_cases_sharing_only_a_common_name_stay_separate() -> None:
    # Enough distinct cases sharing only a common first name → no merge on that
    # token, so they remain separate candidates and the tool disambiguates rather
    # than silently collapsing them into one (and picking one arbitrarily).
    hits = [
        gs_hit("A", "David Alpha - 1"),
        gs_hit("B", "David Bravo - 2"),
        gs_hit("C", "David Charlie - 3"),
        gs_hit("D", "David Delta - 4"),
        gs_hit("E", "David Echo - 5"),
    ]
    result = MatterResolver(estateably=FakeEstateably(hits)).resolve("David")
    assert result.resolved is None  # ambiguous — no single winner
    assert {r.id for c in result.candidates for r in c.refs} == {"A", "B", "C", "D", "E"}
    assert all(len(c.refs) == 1 for c in result.candidates)  # none merged


def test_multi_system_candidate_scores_above_single() -> None:
    resolver = MatterResolver(
        estateably=FakeEstateably([est("E1", "Smith Estate"), est("E2", "Jones Estate")]),
        clio=FakeClio([clio(1, "Smith family estate")]),
    )
    result = resolver.resolve("Smith")
    top = result.candidates[0]
    assert top.name.lower().startswith("smith") or "smith" in top.name.lower()
    assert top.systems == {"estateably", "clio"}  # corroborated wins


def test_one_source_only_still_resolves() -> None:
    resolver = MatterResolver(estateably=FakeEstateably([est("E1", "Estate of Smith")]))
    result = resolver.resolve("Smith estate")
    assert result.resolved is not None
    assert result.resolved.systems == {"estateably"}


def test_failing_source_is_skipped_not_fatal() -> None:
    resolver = MatterResolver(
        estateably=FakeEstateably(RuntimeError("WAF 403")),
        clio=FakeClio([clio(7, "Smith Estate")]),
    )
    result = resolver.resolve("Smith")
    # Estateably blew up but Clio still yields a candidate.
    assert result.resolved is not None
    assert result.resolved.systems == {"clio"}


def test_no_matches_returns_empty() -> None:
    resolver = MatterResolver(estateably=FakeEstateably([]), clio=FakeClio([]))
    result = resolver.resolve("Nonexistent")
    assert result.candidates == []
    assert result.resolved is None


def test_tidy_name_normalizes_hyphen_spacing() -> None:
    # The reported "Estate Administration -Estate of..." gets balanced spacing;
    # a hyphenated surname is left intact.
    assert _tidy_name("Estate Administration -Estate of X") == "Estate Administration - Estate of X"
    assert _tidy_name("Foo- Bar") == "Foo - Bar"
    assert _tidy_name("Smith-Jones") == "Smith-Jones"
    assert _tidy_name("A   B") == "A B"
