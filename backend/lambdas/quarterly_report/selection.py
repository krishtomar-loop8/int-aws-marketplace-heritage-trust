"""Practice-area matter selection for scheduled quarterly runs.

Nicole's decision (2026-08-06): scheduled runs generate statements for open Clio
matters in the qualifying practice areas (Trustee, Committee, Power of Attorney).
A Clio **"Report Exclusion"** custom field (a checkbox — ticked ⇒ ``value: true``)
overrides that: a ticked matter is **excluded**, even if it would otherwise
qualify. It is exclusion-only (a checkbox can't express "force-include a
non-qualifying matter"); to bring an outlier in, recategorize its practice area.
Each run also surfaces any open matter with **no practice area** (and not excluded)
so nothing slips through as the matter set evolves.

Selection is read-only. It lists open matters in one nested ``fields=`` call
(paced by the connector's shared ~50 req/min limiter) and partitions them
client-side — mirroring the AML export's client-side status filter
(``aml_export/quality.is_inactive_status``). No matter is generated here; the
handler feeds the selected ids into the generator.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from services.logging import get_logger

logger = get_logger(__name__)

# One list call hydrates the practice area + custom fields. The exclusion flag is
# matched by ``field_name`` (the direct attribute Clio returns on each value).
# NOTE: Clio v4 rejects a nested ``custom_field{...}`` expansion inside
# ``custom_field_values`` with 400 InvalidFields ("custom_field_values} is not a
# valid field") — verified against live Clio — so we request only the shallow
# ``field_name``. Do not add ``custom_field{...}`` back here.
SELECT_FIELDS = (
    "id,display_number,description,status,client{id,name},"
    "practice_area{id,name},"
    "custom_field_values{id,value,field_name}"
)

# Bound the list well above Aphrodite's ~180 open matters so a "select everything"
# run can never silently truncate (the connector logs a truncation regardless).
_LIST_LIMIT = 2000

# Truthy tokens for the exclusion checkbox (ticked ⇒ bool True, but tolerate a
# text field carrying "Yes"/"true"/"1" too).
_TRUTHY = {"yes", "y", "true", "1"}


@dataclass(frozen=True)
class MatterRow:
    """One open matter, with the fields selection cares about."""

    id: str
    number: str
    client: str
    practice_area: str | None
    report_exclusion: bool  # the "Report Exclusion" checkbox is ticked

    def as_dict(self) -> dict[str, Any]:
        return {
            "matter_id": self.id,
            "matter_number": self.number,
            "client": self.client,
            "practice_area": self.practice_area,
            "report_exclusion": self.report_exclusion,
        }


@dataclass(frozen=True)
class Selection:
    """Open matters partitioned into generate / flag-for-review / exclude."""

    selected: list[MatterRow] = field(default_factory=list)
    flagged_no_area: list[MatterRow] = field(default_factory=list)
    excluded: list[MatterRow] = field(default_factory=list)

    @property
    def selected_ids(self) -> list[str]:
        return [r.id for r in self.selected if r.id]

    def as_dict(self) -> dict[str, Any]:
        return {
            "selected": [r.as_dict() for r in self.selected],
            "flagged_no_practice_area": [r.as_dict() for r in self.flagged_no_area],
            "excluded_count": len(self.excluded),
        }


def _practice_area_name(matter: dict[str, Any]) -> str | None:
    name = str((matter.get("practice_area") or {}).get("name") or "").strip()
    return name or None


def _is_excluded(matter: dict[str, Any], field_name: str) -> bool:
    """Whether the matter's "Report Exclusion" field is ticked (⇒ exclude).

    Matches the configured field by ``field_name``. A checkbox comes through as a
    boolean; a text field carrying "Yes"/"true"/"1" is also honored. Absent or any
    other value ⇒ not excluded (the practice-area rule then applies).
    """
    target = field_name.strip().casefold()
    if not target:
        return False
    for cfv in matter.get("custom_field_values") or []:
        if str(cfv.get("field_name") or "").strip().casefold() != target:
            continue
        value = cfv.get("value")
        if isinstance(value, bool):
            return value
        return str(value).strip().casefold() in _TRUTHY
    return False


def select_matters(
    clio: Any,
    *,
    qualifying: Sequence[str],
    exclusion_field: str,
) -> Selection:
    """List open Clio matters and partition them per Nicole's rule.

    ``qualifying`` — practice-area names that get a statement (case-insensitive).
    ``exclusion_field`` — name of the "Report Exclusion" checkbox; ticked ⇒ excluded
    regardless of practice area.
    """
    qualifying_cf = {q.strip().casefold() for q in qualifying if q and q.strip()}
    if not qualifying_cf:
        logger.warning("no qualifying practice areas configured; selection will be empty")

    rows = clio.list_matters(status="open", fields=SELECT_FIELDS, limit=_LIST_LIMIT)

    selected: list[MatterRow] = []
    flagged: list[MatterRow] = []
    excluded: list[MatterRow] = []

    for m in rows:
        area = _practice_area_name(m)
        report_exclusion = _is_excluded(m, exclusion_field)
        row = MatterRow(
            id=str(m.get("id") or ""),
            number=str(m.get("display_number") or ""),
            client=str((m.get("client") or {}).get("name") or ""),
            practice_area=area,
            report_exclusion=report_exclusion,
        )
        qualifies = area is not None and area.casefold() in qualifying_cf
        if report_exclusion:
            # Report Exclusion ticked ⇒ omit, even if it would otherwise qualify.
            excluded.append(row)
        elif qualifies:
            selected.append(row)
        elif area is None:
            # No practice area (and not excluded) — surfaced so staff can categorize
            # it before the next run (Nicole: "nothing slips through").
            flagged.append(row)
        else:
            excluded.append(row)

    logger.info(
        "practice-area selection",
        extra={
            "extra_fields": {
                "open": len(rows),
                "selected": len(selected),
                "flagged_no_practice_area": len(flagged),
                "excluded": len(excluded),
                "qualifying": sorted(qualifying_cf),
            }
        },
    )
    return Selection(selected=selected, flagged_no_area=flagged, excluded=excluded)
