"""Unit tests for practice-area matter selection (fakes, no network)."""

from __future__ import annotations

from backend.lambdas.quarterly_report.selection import select_matters

QUALIFYING = ("Trustee", "Committee", "Power of Attorney")
FIELD = "Report Exclusion"


class FakeClio:
    def __init__(self, matters):
        self._matters = matters
        self.calls: list[dict] = []

    def list_matters(self, *, status="open", fields=None, limit=500):
        self.calls.append({"status": status, "fields": fields, "limit": limit})
        return list(self._matters)


def _m(id, number, client, *, area=None, custom=None):
    matter = {"id": id, "display_number": number, "client": {"id": id * 10, "name": client}}
    if area is not None:
        matter["practice_area"] = {"id": 1, "name": area}
    if custom is not None:
        matter["custom_field_values"] = custom
    return matter


def _flag(name, value):
    """A custom_field_values entry (Clio returns the shallow field_name form)."""
    return [{"id": 5, "value": value, "field_name": name}]


def _select(matters):
    clio = FakeClio(matters)
    return clio, select_matters(clio, qualifying=QUALIFYING, exclusion_field=FIELD)


def test_qualifying_practice_area_is_selected():
    _, sel = _select([_m(1, "250001", "Trust A", area="Trustee")])
    assert sel.selected_ids == ["1"]
    assert sel.flagged_no_area == []


def test_non_qualifying_practice_area_is_excluded():
    _, sel = _select([_m(2, "250002", "Estate B", area="Executor/Administrator")])
    assert sel.selected_ids == []
    assert [r.id for r in sel.excluded] == ["2"]
    assert sel.flagged_no_area == []


def test_case_insensitive_area_match():
    _, sel = _select([_m(3, "250003", "C", area="power of attorney")])
    assert sel.selected_ids == ["3"]


def test_report_exclusion_excludes_qualifying_matter():
    # Ticked checkbox (bool True) on a qualifying matter ⇒ excluded.
    _, sel = _select([_m(4, "250004", "D", area="Trustee", custom=_flag(FIELD, True))])
    assert sel.selected_ids == []
    assert [r.id for r in sel.excluded] == ["4"]


def test_report_exclusion_on_non_qualifying_stays_excluded():
    _, sel = _select([_m(5, "250005", "E", area="Miscellaneous", custom=_flag(FIELD, True))])
    assert sel.selected_ids == []
    assert [r.id for r in sel.excluded] == ["5"]


def test_report_exclusion_on_no_area_matter_is_excluded_not_flagged():
    # Ticked exclusion on an uncategorized matter is a deliberate omission, not a
    # data gap — so it is excluded and must not appear in the "please categorize" list.
    _, sel = _select([_m(6, "250006", "F", custom=_flag(FIELD, True))])
    assert sel.selected_ids == []
    assert sel.flagged_no_area == []
    assert [r.id for r in sel.excluded] == ["6"]


def test_no_practice_area_and_not_excluded_is_flagged():
    _, sel = _select([_m(7, "250007", "G")])
    assert sel.selected_ids == []
    assert [r.number for r in sel.flagged_no_area] == ["250007"]


def test_unticked_checkbox_applies_practice_area_rule():
    # An unticked checkbox comes through as bool False ⇒ not excluded ⇒ rule applies.
    _, sel = _select(
        [
            _m(8, "250008", "H", area="Trustee", custom=_flag(FIELD, False)),
            _m(9, "250009", "I", area="Miscellaneous", custom=_flag(FIELD, False)),
        ]
    )
    assert sel.selected_ids == ["8"]  # qualifying + unticked ⇒ selected
    assert [r.id for r in sel.excluded] == ["9"]  # non-qualifying ⇒ excluded


def test_text_yes_value_also_excludes():
    # Tolerate a text field carrying "Yes" (in case HT switches field type).
    _, sel = _select([_m(10, "250010", "J", area="Trustee", custom=_flag(FIELD, "Yes"))])
    assert sel.selected_ids == []
    assert [r.id for r in sel.excluded] == ["10"]


def test_unrelated_custom_field_is_ignored():
    _, sel = _select([_m(11, "250011", "K", area="Trustee", custom=_flag("Probate Filed", True))])
    assert sel.selected_ids == ["11"]  # different field ⇒ not excluded ⇒ rule applies


def test_blank_flag_value_falls_back_to_practice_area_rule():
    _, sel = _select([_m(12, "250012", "L", area="Trustee", custom=_flag(FIELD, ""))])
    assert sel.selected_ids == ["12"]


def test_selection_as_dict_reports_report_exclusion():
    _, sel = _select([_m(13, "250013", "M", area="Trustee", custom=_flag(FIELD, True))])
    assert sel.as_dict()["excluded_count"] == 1
    _, sel2 = _select([_m(14, "250014", "N", area="Trustee")])
    assert sel2.as_dict()["selected"][0]["report_exclusion"] is False


def test_list_call_requests_open_status_with_area_and_custom_fields():
    clio, _ = _select([_m(1, "250001", "A", area="Trustee")])
    call = clio.calls[0]
    assert call["status"] == "open"
    assert "practice_area{id,name}" in call["fields"]
    assert "custom_field_values{" in call["fields"]
    # Clio v4 rejects a nested custom_field{...} expansion here with 400
    # InvalidFields — the flag is matched via the shallow field_name instead.
    assert "custom_field{" not in call["fields"]
    assert "field_name" in call["fields"]


def test_empty_qualifying_selects_nothing():
    clio = FakeClio([_m(1, "250001", "A", area="Trustee")])
    sel = select_matters(clio, qualifying=(), exclusion_field=FIELD)
    assert sel.selected_ids == []
