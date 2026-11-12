"""Unit tests for synthesis. The LLM is faked; no AWS/network."""

from __future__ import annotations

import json
from typing import Any

from backend.search.framework import SECTION_KEYS, render_framework
from backend.search.orchestrator import DossierItem, MatterDossier
from backend.search.synthesize import (
    DEFAULT_MAX_TOKENS,
    RETRY_MAX_TOKENS,
    RETRY_TIME_BUDGET_MS,
    MatterSynthesizer,
    _as_str_list,
    _normalize_sections,
)
from services.llm import LLMResult


class FakeLLM:
    """Returns a canned reply; records each call so we can assert prompt/caching."""

    def __init__(self, text: str, stop_reason: str = "end_turn") -> None:
        self._text = text
        self._stop_reason = stop_reason
        self.calls: list[dict[str, Any]] = []

    def synthesize(self, prompt: str, **kwargs: Any) -> LLMResult:
        self.calls.append({"prompt": prompt, **kwargs})
        return LLMResult(
            text=self._text,
            model_id="test-model",
            input_tokens=100,
            output_tokens=50,
            stop_reason=self._stop_reason,
        )


class SequenceLLM:
    """Returns a different (text, stop_reason) per call — for retry tests."""

    def __init__(self, replies: list[tuple[str, str]]) -> None:
        self._replies = replies
        self.calls: list[dict[str, Any]] = []

    def synthesize(self, prompt: str, **kwargs: Any) -> LLMResult:
        text, stop = self._replies[min(len(self.calls), len(self._replies) - 1)]
        self.calls.append({"prompt": prompt, **kwargs})
        return LLMResult(
            text=text,
            model_id="test-model",
            input_tokens=100,
            output_tokens=50,
            stop_reason=stop,
        )


def make_dossier() -> MatterDossier:
    d = MatterDossier(matter_name="Smith Estate", matter_type="estate")
    d.section("tasks").items.append(DossierItem("clio", "9", "task: file T1", "2026-04-30"))
    d.section("kyc").items.append(DossierItem("estateably", "D1", "doc: Will.pdf", None))
    d.section("tasks").items.append(DossierItem("estateably", "-", "(+5 more)"))  # marker row
    return d


def _sections(**over: str) -> dict[str, str]:
    secs = {k: f"{title} text" for k, title in SECTION_KEYS}
    secs.update(over)
    return secs


def good_reply(**overrides: Any) -> str:
    payload: dict[str, Any] = {
        "sections": _sections(),
        "sources_reviewed": ["Clio tasks"],
        "insufficient_info": False,
    }
    payload.update(overrides)
    return json.dumps(payload)


# -- prompt + caching -------------------------------------------------------


def test_uses_cached_framework_prefix_and_includes_dossier() -> None:
    llm = FakeLLM(good_reply())
    MatterSynthesizer(llm).synthesize(make_dossier(), "status on Smith estate?")
    call = llm.calls[0]
    assert call["cache_system"] == render_framework()  # static prefix → cacheable
    assert "Smith Estate" in call["prompt"]
    assert "status on Smith estate?" in call["prompt"]
    assert '"record_id":"9"' in call["prompt"] or '"9"' in call["prompt"]


def test_synthesizer_uses_custom_org_config() -> None:
    llm = FakeLLM(good_reply())
    synth = MatterSynthesizer(llm, org_name="Acme Trust", office_address="1 Main St")
    synth.synthesize(make_dossier(), "status?")
    assert "Acme Trust" in llm.calls[0]["cache_system"]
    assert "Aphrodite" not in llm.calls[0]["cache_system"]


def test_synthesize_override_replaces_the_constructed_framework() -> None:
    llm = FakeLLM(good_reply())
    synth = MatterSynthesizer(llm, org_name="Constructed Co", office_address="545 Clyde")
    synth.synthesize(make_dossier(), "status?", org_name="Acme Trust", office_address="1 Main St")
    assert llm.calls[0]["cache_system"] == render_framework("Acme Trust", "1 Main St")
    assert "Constructed Co" not in llm.calls[0]["cache_system"]


def test_synthesize_override_of_one_field_keeps_the_other_constructed_value() -> None:
    llm = FakeLLM(good_reply())
    synth = MatterSynthesizer(llm, org_name="Constructed Co", office_address="545 Clyde")
    synth.synthesize(make_dossier(), "status?", org_name="Acme Trust")
    assert llm.calls[0]["cache_system"] == render_framework("Acme Trust", "545 Clyde")


def test_synthesize_override_honors_an_explicit_empty_value() -> None:
    # Presence, not truthiness: a value explicitly passed (even "") is the
    # configured value, not a cue to fall back to the constructed one.
    llm = FakeLLM(good_reply())
    synth = MatterSynthesizer(llm, org_name="Constructed Co", office_address="545 Clyde")
    synth.synthesize(make_dossier(), "status?", org_name="", office_address="")
    assert llm.calls[0]["cache_system"] == render_framework("", "")
    assert "Constructed Co" not in llm.calls[0]["cache_system"]


def test_synthesize_override_applies_to_the_truncation_retry_too() -> None:
    llm = SequenceLLM([("{truncated", "max_tokens"), (good_reply(), "end_turn")])
    synth = MatterSynthesizer(llm, org_name="Constructed Co", office_address="545 Clyde")
    synth.synthesize(make_dossier(), "status?", org_name="Acme Trust", office_address="1 Main St")
    assert len(llm.calls) == 2
    expected = render_framework("Acme Trust", "1 Main St")
    assert [c["cache_system"] for c in llm.calls] == [expected, expected]


def test_synthesize_override_does_not_stick_to_the_next_call() -> None:
    llm = FakeLLM(good_reply())
    synth = MatterSynthesizer(llm, org_name="Constructed Co", office_address="545 Clyde")
    synth.synthesize(make_dossier(), "status?", org_name="Acme Trust", office_address="1 Main St")
    synth.synthesize(make_dossier(), "status?")
    assert llm.calls[1]["cache_system"] == render_framework("Constructed Co", "545 Clyde")


def test_synthesize_without_override_uses_the_constructed_framework() -> None:
    llm = FakeLLM(good_reply())
    synth = MatterSynthesizer(llm, org_name="Constructed Co", office_address="545 Clyde")
    synth.synthesize(make_dossier(), "status?")
    assert llm.calls[0]["cache_system"] == render_framework("Constructed Co", "545 Clyde")


def test_framework_carries_the_fidelity_and_tax_rules() -> None:
    # Canaries for the feedback-round-2 hardening: these rules were added after
    # real client-reported failures — an accidental edit that drops one should
    # fail loudly here, not silently regress in production summaries.
    fw = " ".join(render_framework().split())  # collapse line wrapping
    for canary in (
        # correspondence fidelity (model-authored summary, fabrication, inversion)
        "# CORRESPONDENCE FIDELITY (Outlook)",
        'Begin the "outlook_correspondence" section',
        "NEVER attribute a statement",
        "not found in reviewed correspondence",
        "Never invert an instruction",
        "already Pacific time",
        # speculation / filenames / read-only recommendations
        "Do NOT expand or interpret abbreviations",
        "Never read dollar amounts, dates, or facts out of file NAMES",
        "Never recommend creating, editing, or deleting records",
        # roles + c/o address
        "administrator with will annexed",
        "545 Clyde Avenue",
        # tax: terminal vs prior-year + report the code-computed T1 deadline; T3-only caution
        "TERMINAL T1 (the year-of-death return)",
        'report THAT date verbatim',
        "applies to T3 ESTATE returns only",
        # output contract: no top-level sources_reviewed; insufficient_info criteria
        'Do NOT emit a top-level "sources_reviewed" array',
        'Set "insufficient_info" to true ONLY when a core source',
    ):
        assert canary in fw, canary


# -- per-line correspondence citations --------------------------------------


def test_model_authored_correspondence_inline_markers_ground() -> None:
    # The model authors outlook_correspondence as a concise plain-text summary with an
    # inline (outlook:<short-id>) marker on each statement. The section flows through
    # unchanged (no backend override) and every marker grounds against the dossier's
    # correspondence items — identically to how the analysis sections are cited.
    d = make_dossier()
    corr = d.section("correspondence")
    corr.items.append(
        DossierItem(
            "outlook",
            "ol-aaaa1111",
            "RE: Smith — Ken confirmed the lawsuit was filed",
            "2026-07-29",
            {"url": "u1", "body": "Ken: the claim was filed today."},
        )
    )
    corr.items.append(
        DossierItem(
            "outlook",
            "ol-bbbb2222",
            "RE: Smith — Nada requested addresses",
            "2026-07-28",
            {"url": "u2", "body": "Nada: please send the beneficiary addresses."},
        )
    )
    summary = (
        "Outlook correspondence reviewed to 2026-07-29 10:00 am Pacific — 2 of 5\n"
        "- Ken confirmed the lawsuit was filed (outlook:ol-aaaa1111).\n"
        "- Nada requested the beneficiary addresses (outlook:ol-bbbb2222)."
    )
    reply = good_reply(sections=_sections(outlook_correspondence=summary))
    res = MatterSynthesizer(FakeLLM(reply)).synthesize(d, "q")
    # model-authored string flows through unchanged (markers kept inline for the SPA)
    assert res.sections["outlook_correspondence"] == summary
    cited = {(c["system"], c["record_id"]) for c in res.citations}
    assert ("outlook", "ol-aaaa1111") in cited and ("outlook", "ol-bbbb2222") in cited
    # the grounded citation carries the email's deep-link url from the dossier record
    by_ref = {(c["system"], c["record_id"]): c for c in res.citations}
    assert by_ref[("outlook", "ol-aaaa1111")].get("url") == "u1"


# -- citations built from inline markers ------------------------------------


def test_grounded_marker_gets_source_url() -> None:
    d = MatterDossier(matter_name="Smith Estate", matter_type="estate")
    d.section("tasks").items.append(
        DossierItem(
            "clio",
            "9",
            "task: file T1",
            "2026-04-30",
            {"url": "https://app.clio.com/nc/#/matters/42"},
        )
    )
    reply = good_reply(sections=_sections(current_stage="Terminal T1 filed (clio:9)."))
    result = MatterSynthesizer(FakeLLM(reply)).synthesize(d, "q")
    cite = next(c for c in result.citations if c["record_id"] == "9")
    assert cite["system"] == "clio"
    assert cite["url"] == "https://app.clio.com/nc/#/matters/42"


def test_citation_note_from_record_and_no_url_when_absent() -> None:
    reply = good_reply(sections=_sections(current_stage="Terminal T1 filed (clio:9)."))
    result = MatterSynthesizer(FakeLLM(reply)).synthesize(make_dossier(), "q")
    cite = next(c for c in result.citations if c["record_id"] == "9")
    assert cite["note"] == "task: file T1"  # note derived from the dossier record
    assert "url" not in cite  # the dossier item carried no url


def test_correspondence_citation_carries_excerpt_and_date_for_in_app_evidence() -> None:
    # An Outlook item's masked body excerpt + date ride along on the citation so a reader
    # can verify the email in-app (the deep-link only opens with mailbox access). Items
    # without a body (Estateably/Clio/QB) carry no excerpt.
    d = make_dossier()
    d.section("correspondence").items.append(
        DossierItem(
            "outlook",
            "ol-aaaa1111",
            "RE: Smith — from Ken (mailbox: nada@ht.ca)",
            "2026-07-29",
            {"url": "https://outlook.office365.com/owa/?ItemID=X", "body": "Ken: claim filed."},
        )
    )
    reply = good_reply(
        sections=_sections(current_stage="Filed (outlook:ol-aaaa1111); task (clio:9).")
    )
    res = MatterSynthesizer(FakeLLM(reply)).synthesize(d, "q")
    by_ref = {(c["system"], c["record_id"]): c for c in res.citations}
    ol = by_ref[("outlook", "ol-aaaa1111")]
    assert ol["excerpt"] == "Ken: claim filed."  # in-app evidence
    assert ol["date"] == "2026-07-29"
    assert ol["url"] == "https://outlook.office365.com/owa/?ItemID=X"  # secondary open-in-Outlook
    assert "excerpt" not in by_ref[("clio", "9")]  # non-correspondence item has no body


def test_grounded_marker_is_kept() -> None:
    reply = good_reply(sections=_sections(current_stage="Terminal T1 filed (clio:9)."))
    res = MatterSynthesizer(FakeLLM(reply)).synthesize(make_dossier(), "q")
    assert res.citations == [{"system": "clio", "record_id": "9", "note": "task: file T1"}]
    assert res.invalid_citations == []


def test_ungrounded_marker_is_flagged() -> None:
    reply = good_reply(
        sections=_sections(
            current_stage="Filed (clio:9); per (estateably:FAKE); see (estateably:-).",
        )
    )
    res = MatterSynthesizer(FakeLLM(reply)).synthesize(make_dossier(), "q")
    assert {"system": "clio", "record_id": "9", "note": "task: file T1"} in res.citations
    ungrounded = {(c["system"], c["record_id"]) for c in res.invalid_citations}
    assert ("estateably", "FAKE") in ungrounded  # not a record in the dossier
    assert ("estateably", "-") in ungrounded  # the "(+5 more)" marker row isn't citable


def test_repeated_marker_is_deduped() -> None:
    reply = good_reply(
        sections=_sections(current_stage="(clio:9)", completed_items="also (clio:9)")
    )
    res = MatterSynthesizer(FakeLLM(reply)).synthesize(make_dossier(), "q")
    assert [c["record_id"] for c in res.citations] == ["9"]


def test_continuation_id_inherits_system() -> None:
    # "(clio:9; D1)" — the bare "D1" has no prefix; it must NOT inherit clio (it's
    # an estateably record), so it grounds only if written as its own system.
    d = MatterDossier(matter_name="Smith Estate", matter_type="estate")
    d.section("tasks").items.append(DossierItem("clio", "9", "file T1", None))
    d.section("kyc").items.append(DossierItem("clio", "D1", "clio doc", None))
    reply = good_reply(sections=_sections(current_stage="Both (clio:9; D1)."))
    res = MatterSynthesizer(FakeLLM(reply)).synthesize(d, "q")
    ids = {(c["system"], c["record_id"]) for c in res.citations}
    assert ("clio", "9") in ids and ("clio", "D1") in ids  # D1 inherited "clio"


# -- parsing + sections -----------------------------------------------------


def test_parses_all_sections() -> None:
    llm = FakeLLM(good_reply())
    res = MatterSynthesizer(llm).synthesize(make_dossier(), "q")
    assert res.parse_ok
    assert set(res.sections) == {k for k, _ in SECTION_KEYS}
    assert not res.missing_sections
    assert res.input_tokens == 100 and res.output_tokens == 50
    assert res.model_id == "test-model"
    assert res.stop_reason == "end_turn" and res.retried is False
    assert len(llm.calls) == 1  # a clean parse never retries


def test_json_wrapped_in_prose_is_extracted() -> None:
    wrapped = "Here you go:\n```json\n" + good_reply() + "\n```\nHope that helps."
    res = MatterSynthesizer(FakeLLM(wrapped)).synthesize(make_dossier(), "q")
    assert res.parse_ok
    assert res.sections["current_stage"] == "Current stage text"


def test_unparseable_reply_flagged_after_retry() -> None:
    llm = FakeLLM("the model rambled, no json")
    res = MatterSynthesizer(llm).synthesize(make_dossier(), "q")
    assert res.parse_ok is False
    assert res.sections == {}
    assert res.raw_text == "the model rambled, no json"
    assert res.retried is True  # unparseable → one retry attempt
    assert len(llm.calls) == 2


def test_missing_sections_reported() -> None:
    reply = good_reply(sections={"current_stage": "only one"})
    res = MatterSynthesizer(FakeLLM(reply)).synthesize(make_dossier(), "q")
    assert "tax_status" in res.missing_sections
    assert "current_stage" not in res.missing_sections


def test_synthesis_budget_constants() -> None:
    # The client asked to favor completeness over latency ("hit the max limit if
    # it results in better output"); these are deliberately sized against the
    # search Lambda's fixed 900s AWS timeout (not ours to raise) and an observed
    # ~28-33ms/output-token generation rate, not the model's own much higher
    # (128k, live-verified) ceiling. A careless bump toward that model ceiling
    # would risk the Lambda's own timeout killing an in-flight generation.
    assert DEFAULT_MAX_TOKENS == 22000
    assert RETRY_MAX_TOKENS == 26000
    assert RETRY_TIME_BUDGET_MS == 510_000


# -- truncation retry (Layer 4) ---------------------------------------------


def test_retry_on_max_tokens_recovers() -> None:
    truncated = '{"sections": {"current_stage": "Early admin, T1 fil'  # cut off mid-JSON
    llm = SequenceLLM([(truncated, "max_tokens"), (good_reply(), "end_turn")])
    res = MatterSynthesizer(llm, max_tokens=1000).synthesize(make_dossier(), "q")
    assert res.retried is True
    assert res.parse_ok is True
    assert set(res.sections) == {k for k, _ in SECTION_KEYS}
    assert len(llm.calls) == 2
    assert llm.calls[0]["max_tokens"] == 1000
    assert llm.calls[1]["max_tokens"] == 2000  # doubled on retry


def test_no_retry_when_first_reply_parses() -> None:
    llm = SequenceLLM([(good_reply(), "end_turn")])
    res = MatterSynthesizer(llm).synthesize(make_dossier(), "q")
    assert res.retried is False
    assert len(llm.calls) == 1


def test_retry_budget_is_capped() -> None:
    llm = SequenceLLM([("garbage cut off", "max_tokens"), (good_reply(), "end_turn")])
    MatterSynthesizer(llm, max_tokens=20000).synthesize(make_dossier(), "q")
    assert llm.calls[1]["max_tokens"] == RETRY_MAX_TOKENS  # min(40000, RETRY_MAX_TOKENS)


def test_no_retry_loop_when_already_at_cap() -> None:
    # max_tokens already at the retry ceiling → no room to grow → no retry.
    llm = SequenceLLM([("garbage cut off", "max_tokens")])
    res = MatterSynthesizer(llm, max_tokens=RETRY_MAX_TOKENS).synthesize(make_dossier(), "q")
    assert res.retried is False
    assert len(llm.calls) == 1


def test_retry_skipped_when_time_budget_low() -> None:
    # Truncated, but not enough Lambda time remains for another full generation →
    # skip the retry so it can't run past the function timeout and strand the job.
    truncated = '{"sections": {"current_stage": "cut'
    llm = SequenceLLM([(truncated, "max_tokens"), (good_reply(), "end_turn")])
    res = MatterSynthesizer(llm, max_tokens=1000).synthesize(
        make_dossier(),
        "q",
        remaining_ms=lambda: 1_000.0,  # far below RETRY_TIME_BUDGET_MS
    )
    assert res.retried is False
    assert res.parse_ok is False
    assert len(llm.calls) == 1


def test_retry_runs_when_time_budget_ample() -> None:
    truncated = '{"sections": {"current_stage": "cut'
    llm = SequenceLLM([(truncated, "max_tokens"), (good_reply(), "end_turn")])
    res = MatterSynthesizer(llm, max_tokens=1000).synthesize(
        make_dossier(),
        "q",
        remaining_ms=lambda: 600_000.0,  # ample
    )
    assert res.retried is True
    assert res.parse_ok is True
    assert len(llm.calls) == 2


# -- type coercion of model output (blank-page crash guard) -----------------
# The model sometimes emits a section value as a JSON array (esp. sources_reviewed,
# which is also a top-level array key). The SPA renders each section value as text
# and would crash on a non-string, so the synthesizer coerces it here.


def test_list_section_value_is_coerced_to_bulleted_string() -> None:
    reply = good_reply(
        sections=_sections(sources_reviewed=["Estateably — summary", "Clio — matter 9"])
    )
    res = MatterSynthesizer(FakeLLM(reply)).synthesize(make_dossier(), "q")
    value = res.sections["sources_reviewed"]
    assert isinstance(value, str)  # never a list → the SPA can .split() it safely
    assert value == "- Estateably — summary\n- Clio — matter 9"


def test_all_section_values_are_strings() -> None:
    # A mix of the bad types the model has produced in the wild.
    reply = good_reply(
        sections=_sections(
            completed_items=["a", "b"],
            risks_blockers={"nested": "object"},
        )
    )
    res = MatterSynthesizer(FakeLLM(reply)).synthesize(make_dossier(), "q")
    assert all(isinstance(v, str) for v in res.sections.values())


def test_top_level_sources_reviewed_coerced_to_list() -> None:
    # Model returns the top-level sources_reviewed as a bare string, not an array.
    reply = good_reply(sources_reviewed="Clio tasks and notes")
    res = MatterSynthesizer(FakeLLM(reply)).synthesize(make_dossier(), "q")
    assert res.sources_reviewed == ["Clio tasks and notes"]  # SPA .map()s a list


def test_normalize_sections_helper() -> None:
    out = _normalize_sections(
        {
            "a": "already text",
            "b": ["x", "y", "  "],  # blank items dropped
            "c": 42,
            "d": None,  # dropped
        }
    )
    assert out == {"a": "already text", "b": "- x\n- y", "c": "42"}
    assert _normalize_sections(None) == {}  # non-dict → empty
    assert _normalize_sections(["not", "a", "dict"]) == {}


def test_as_str_list_helper() -> None:
    assert _as_str_list(["a", "b"]) == ["a", "b"]
    assert _as_str_list("one") == ["one"]
    assert _as_str_list(None) == []
    assert _as_str_list({"x": 1}) == []
    assert _as_str_list([1, 2]) == ["1", "2"]
