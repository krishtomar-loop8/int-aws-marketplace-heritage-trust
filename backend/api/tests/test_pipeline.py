"""Unit tests for the search pipeline. Everything is faked; no AWS/network."""

from __future__ import annotations

from typing import Any

from backend.api.pipeline import run_resolve, run_search
from backend.search.orchestrator import DossierItem, MatterDossier
from backend.search.resolve import MatterCandidate, ResolutionResult, SourceRef
from backend.search.synthesize import SynthesisResult


class FakeResolver:
    def __init__(self, result: ResolutionResult) -> None:
        self._result = result

    def resolve(self, query: str, **_: Any) -> ResolutionResult:
        return self._result


class FakeBuilder:
    def __init__(self, dossier: MatterDossier) -> None:
        self._dossier = dossier
        self.built: list[MatterCandidate] = []

    def build(self, candidate: MatterCandidate, **_: Any) -> MatterDossier:
        self.built.append(candidate)
        return self._dossier


class FakeSynth:
    def __init__(self, result: SynthesisResult) -> None:
        self._result = result

    def synthesize(self, dossier: MatterDossier, query: str, **_: Any) -> SynthesisResult:
        return self._result


def _candidate(name: str = "Smith Estate") -> MatterCandidate:
    return MatterCandidate(
        name=name,
        refs=[SourceRef("estateably", "E1", "Smith"), SourceRef("clio", "9", "Smith")],
        matter_type="estate",
        score=1.0,
    )


def _resolution(cands: list[MatterCandidate]) -> ResolutionResult:
    return ResolutionResult(query="q", term="smith", matter_type="estate", candidates=cands)


def _synth() -> SynthesisResult:
    return SynthesisResult(
        sections={"current_stage": "early admin"},
        citations=[{"system": "clio", "record_id": "9"}],
        invalid_citations=[],
        sources_reviewed=["Clio"],
        insufficient_info=False,
        model_id="test-model",
        input_tokens=100,
        output_tokens=40,
        raw_text="{}",
    )


# -- resolve ----------------------------------------------------------------


def test_run_resolve_returns_candidates() -> None:
    out = run_resolve("status on Smith", resolver=FakeResolver(_resolution([_candidate()])))
    assert out["term"] == "smith"
    assert out["candidates"][0]["name"] == "Smith Estate"
    assert out["candidates"][0]["systems"] == ["clio", "estateably"]


# -- search -----------------------------------------------------------------


def test_search_not_found() -> None:
    out = run_search(
        "q",
        resolver=FakeResolver(_resolution([])),
        dossier_builder=FakeBuilder(MatterDossier("x", None)),
        synthesizer=FakeSynth(_synth()),
    )
    assert out["status"] == "not_found"


def test_search_disambiguation_when_multiple() -> None:
    cands = [_candidate("Smith Estate"), _candidate("Smithson Estate")]
    out = run_search(
        "q",
        resolver=FakeResolver(_resolution(cands)),
        dossier_builder=FakeBuilder(MatterDossier("x", None)),
        synthesizer=FakeSynth(_synth()),
    )
    assert out["status"] == "disambiguation"
    assert len(out["candidates"]) == 2


def test_search_ok_runs_dossier_and_synthesis() -> None:
    dossier = MatterDossier("Smith Estate", "estate")
    dossier.section("tasks").items.append(DossierItem("clio", "9", "file T1"))
    dossier.sources_reviewed = ["Clio tasks"]
    builder = FakeBuilder(dossier)

    out = run_search(
        "status on Smith estate?",
        resolver=FakeResolver(_resolution([_candidate()])),
        dossier_builder=builder,
        synthesizer=FakeSynth(_synth()),
        actor={"sub": "user-123"},
    )
    assert out["status"] == "ok"
    assert out["matter"]["name"] == "Smith Estate"
    assert out["answer"]["sections"]["current_stage"] == "early admin"
    assert out["answer"]["citations"] == [{"system": "clio", "record_id": "9"}]
    assert out["usage"]["input_tokens"] == 100
    assert builder.built  # dossier was actually built for the resolved candidate


def test_sources_reviewed_is_deterministic_not_model_echo() -> None:
    # The model's echoed sources_reviewed used to override the dossier's factual
    # list, dropping notes like "N off-matter excluded" — the dossier list wins.
    dossier = MatterDossier("Smith Estate", "estate")
    dossier.sources_reviewed = [
        "Outlook correspondence reviewed to 2026-07-05 3:00 pm Pacific — 2 unique messages",
        "SharePoint: 0 document excerpt(s) retrieved (matter-scoped); 40 off-matter excluded",
    ]
    out = run_search(
        "q",
        resolver=FakeResolver(_resolution([_candidate()])),
        dossier_builder=FakeBuilder(dossier),
        synthesizer=FakeSynth(_synth()),  # model echoes ["Clio"]
    )
    assert out["answer"]["sources_reviewed"] == dossier.sources_reviewed


def test_insufficient_info_is_derived_not_model_reported() -> None:
    # Model says insufficient on a clean dossier → payload stays false (the model
    # flagged nearly every run); a failed CORE source pull → true, with reasons.
    clean = MatterDossier("Smith Estate", "estate")
    synth = _synth()
    synth.insufficient_info = True  # model over-reports
    out = run_search(
        "q",
        resolver=FakeResolver(_resolution([_candidate()])),
        dossier_builder=FakeBuilder(clean),
        synthesizer=FakeSynth(synth),
    )
    assert out["answer"]["insufficient_info"] is False
    assert out["answer"]["insufficient_reasons"] == []

    broken = MatterDossier("Smith Estate", "estate")
    broken.errors = ["outlook: 3 mailbox searches failed", "sharepoint: throttled"]
    out = run_search(
        "q",
        resolver=FakeResolver(_resolution([_candidate()])),
        dossier_builder=FakeBuilder(broken),
        synthesizer=FakeSynth(_synth()),  # model says fine — dossier says otherwise
    )
    assert out["answer"]["insufficient_info"] is True
    # Only the CORE source failure is a reason; supplementary SharePoint isn't.
    assert out["answer"]["insufficient_reasons"] == ["outlook: 3 mailbox searches failed"]


def test_quickbooks_outage_flags_insufficient_only_when_reconciliation_expected() -> None:
    qb_error = "QuickBooks unavailable — reconciliation data missing (token refresh failed: 400)"
    dossier = MatterDossier("Smith Estate", "estate")
    dossier.errors = [qb_error]

    # No Clio client-contact id on the candidate → no reconciliation expected →
    # a QuickBooks outage must not raise the banner.
    out = run_search(
        "q",
        resolver=FakeResolver(_resolution([_candidate()])),
        dossier_builder=FakeBuilder(dossier),
        synthesizer=FakeSynth(_synth()),
    )
    assert out["answer"]["insufficient_info"] is False

    # With a client-contact id, reconciliation was expected → banner + reason.
    with_qb = MatterCandidate(
        name="Smith Estate",
        refs=[SourceRef("clio", "9", "Smith", qb_customer_id="C77")],
        matter_type="estate",
        score=1.0,
    )
    out = run_search(
        "q",
        resolver=FakeResolver(_resolution([with_qb])),
        dossier_builder=FakeBuilder(dossier),
        synthesizer=FakeSynth(_synth()),
    )
    assert out["answer"]["insufficient_info"] is True
    assert out["answer"]["insufficient_reasons"] == [qb_error]


def _synth_priced() -> SynthesisResult:
    return SynthesisResult(
        sections={"current_stage": "early admin"},
        citations=[],
        invalid_citations=[],
        sources_reviewed=["Clio"],
        insufficient_info=False,
        model_id="us.anthropic.claude-sonnet-4-6",
        input_tokens=2_000,
        output_tokens=7_000,
        raw_text="{}",
        cache_read_tokens=54_000,
        cache_write_tokens=0,
    )


def test_search_ok_reports_cost_cache_tokens_and_timings() -> None:
    dossier = MatterDossier("Smith Estate", "estate")
    out = run_search(
        "status on Smith estate?",
        resolver=FakeResolver(_resolution([_candidate()])),
        dossier_builder=FakeBuilder(dossier),
        synthesizer=FakeSynth(_synth_priced()),
        actor={"sub": "user-123"},
    )
    usage = out["usage"]
    assert usage["cache_read_tokens"] == 54_000
    # Priced model → a real dollar cost is computed from the token buckets.
    expected = (2_000 * 3.0 + 7_000 * 15.0 + 54_000 * 0.30) / 1_000_000
    assert usage["cost_usd"] == round(expected, 6)
    # Timings block is present for the A/B runner to read directly.
    assert set(out["timings"]) == {"resolve_ms", "build_ms", "synth_ms", "source_ms"}


class ExplodingResolver:
    def resolve(self, query: str, **_: Any) -> ResolutionResult:
        raise AssertionError("resolver must not run when a selection is supplied")


def test_search_with_selection_bypasses_resolver() -> None:
    dossier = MatterDossier("Estate of Jane Doe", "estate")
    builder = FakeBuilder(dossier)
    selection = {
        "name": "Estate of Jane Doe",
        "matter_type": "estate",
        "refs": [
            {"system": "estateably", "id": "E7", "label": "Estate of Jane Doe"},
            {"system": "clio", "id": "5567", "label": "00088-Doe", "qb_customer_id": "2369563557"},
        ],
    }
    out = run_search(
        "Estate of Jane Doe",
        resolver=ExplodingResolver(),
        dossier_builder=builder,
        synthesizer=FakeSynth(_synth()),
        selection=selection,
    )
    assert out["status"] == "ok"
    assert out["matter"]["name"] == "Estate of Jane Doe"
    # dossier built for the exact picked candidate, both refs preserved
    built = builder.built[0]
    assert {r.system for r in built.refs} == {"estateably", "clio"}
    assert {r.id for r in built.refs} == {"E7", "5567"}
    # The Clio client-contact id survives the disambiguation round-trip — losing
    # it here is why QuickBooks matching failed on multi-matter clients (only
    # they go through the pick) while single-shot resolutions worked.
    clio_ref = next(r for r in built.refs if r.system == "clio")
    assert clio_ref.qb_customer_id == "2369563557"


def test_candidate_json_carries_qb_customer_id() -> None:
    cand = MatterCandidate(
        name="Kober Estate",
        refs=[SourceRef("clio", "123", "Kober", qb_customer_id="CID9")],
        matter_type="estate",
        score=1.0,
    )
    out = run_resolve("kober", resolver=FakeResolver(_resolution([cand, _candidate()])))
    ref = out["candidates"][0]["refs"][0]
    assert ref["qb_customer_id"] == "CID9"
    # refs without one carry an explicit null (mirrors the dataclass default)
    other = out["candidates"][1]["refs"][0]
    assert other["qb_customer_id"] is None


def test_selection_without_qb_customer_id_still_works() -> None:
    # Selections stored before the field existed (old sessions) must rebuild
    # exactly as before: qb_customer_id None → name-fallback matching.
    builder = FakeBuilder(MatterDossier("Estate of Jane Doe", "estate"))
    selection = {
        "name": "Estate of Jane Doe",
        "matter_type": "estate",
        "refs": [{"system": "clio", "id": "5567", "label": "00088-Doe"}],
    }
    out = run_search(
        "Estate of Jane Doe",
        resolver=ExplodingResolver(),
        dossier_builder=builder,
        synthesizer=FakeSynth(_synth()),
        selection=selection,
    )
    assert out["status"] == "ok"
    assert builder.built[0].refs[0].qb_customer_id is None


class FakeQuick:
    def __init__(self, answer: Any = None) -> None:
        self._answer = answer
        self.calls: list[tuple[str, str]] = []

    def answer(
        self,
        intent: str,
        candidate: MatterCandidate,
        *,
        term: str | None = None,
        query: str | None = None,
    ) -> Any:
        self.calls.append((intent, candidate.name))
        return self._answer


def test_quick_answer_short_circuits_before_synthesis() -> None:
    from backend.search.quick_answers import QuickAnswer

    builder = FakeBuilder(MatterDossier("Smith Estate", "estate"))
    quick = FakeQuick(
        QuickAnswer(
            "balance", "Balance is $5,000. (clio:9)", [{"system": "clio", "record_id": "9"}]
        )
    )
    out = run_search(
        "what's the trust balance on Smith?",
        resolver=FakeResolver(_resolution([_candidate()])),
        dossier_builder=builder,
        synthesizer=FakeSynth(_synth()),
        quick=quick,
    )
    assert out["status"] == "answer"
    assert out["quick"]["intent"] == "balance"
    assert quick.calls == [("balance", "Smith Estate")]
    assert builder.built == []  # dossier/synthesis never ran


def test_status_query_falls_through_to_synthesis() -> None:
    builder = FakeBuilder(MatterDossier("Smith Estate", "estate"))
    quick = FakeQuick(object())  # would answer, but must not be consulted
    out = run_search(
        "what's the status of Smith?",
        resolver=FakeResolver(_resolution([_candidate()])),
        dossier_builder=builder,
        synthesizer=FakeSynth(_synth()),
        quick=quick,
    )
    assert out["status"] == "ok"
    assert quick.calls == []  # classify() returned None → quick never called
    assert builder.built  # full synthesis ran


def test_full_flag_forces_synthesis_even_for_narrow_query() -> None:
    from backend.search.quick_answers import QuickAnswer

    builder = FakeBuilder(MatterDossier("Smith Estate", "estate"))
    quick = FakeQuick(QuickAnswer("balance", "x", []))
    out = run_search(
        "what's the trust balance on Smith?",
        resolver=FakeResolver(_resolution([_candidate()])),
        dossier_builder=builder,
        synthesizer=FakeSynth(_synth()),
        quick=quick,
        full=True,
    )
    assert out["status"] == "ok"
    assert quick.calls == []
    assert builder.built


def test_run_search_emits_phases_in_order() -> None:
    phases: list[str] = []
    run_search(
        "status on Smith estate?",
        resolver=FakeResolver(_resolution([_candidate()])),
        dossier_builder=FakeBuilder(MatterDossier("Smith Estate", "estate")),
        synthesizer=FakeSynth(_synth()),
        on_phase=phases.append,
    )
    assert phases == [
        "Finding the matter",
        "Reading Estateably, Clio and Outlook",
        "Drafting the source-cited summary",
    ]


def test_quick_answer_emits_no_synthesis_phases() -> None:
    from backend.search.quick_answers import QuickAnswer

    phases: list[str] = []
    out = run_search(
        "what's the trust balance on Smith?",
        resolver=FakeResolver(_resolution([_candidate()])),
        dossier_builder=FakeBuilder(MatterDossier("Smith Estate", "estate")),
        synthesizer=FakeSynth(_synth()),
        quick=FakeQuick(QuickAnswer("balance", "x", [])),
        on_phase=phases.append,
    )
    assert out["status"] == "answer"
    assert "Reading Estateably, Clio and Outlook" not in phases
    assert "Drafting the source-cited summary" not in phases


def test_quick_decline_falls_through_to_synthesis() -> None:
    builder = FakeBuilder(MatterDossier("Smith Estate", "estate"))
    quick = FakeQuick(None)  # handler declines
    out = run_search(
        "who are the beneficiaries on Smith?",
        resolver=FakeResolver(_resolution([_candidate()])),
        dossier_builder=builder,
        synthesizer=FakeSynth(_synth()),
        quick=quick,
    )
    assert out["status"] == "ok"
    assert quick.calls == [("parties", "Smith Estate")]
    assert builder.built


def test_search_with_empty_selection_falls_back_to_resolver() -> None:
    # A selection carrying no usable refs must not bypass; normal resolution runs.
    out = run_search(
        "status on Smith",
        resolver=FakeResolver(_resolution([_candidate()])),
        dossier_builder=FakeBuilder(MatterDossier("Smith Estate", "estate")),
        synthesizer=FakeSynth(_synth()),
        selection={"name": "x", "matter_type": None, "refs": []},
    )
    assert out["status"] == "ok"  # resolver ran and resolved the single candidate
