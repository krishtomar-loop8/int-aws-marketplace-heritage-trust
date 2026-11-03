"""Customer-maintained include/exclude designation list for the AML export.

Staff maintain a single spreadsheet with two sections (as two sheets, or two
labelled tables — we parse by column header, not sheet name):

  * Matters — ``Matter Number`` + ``Designation`` (include | exclude) + ``Reason``
  * Parties — ``Party Name`` (+ optional ``Reason``); listing implies exclusion

The list takes **precedence** over the rule-based backstop in :mod:`quality`:
a listed *exclude* removes the matter, a listed *include* keeps it (overriding a
backstop rule), and a listed party name is always excluded from screening. A
matter that is on neither the list nor a backstop rule is kept but **flagged**
("not on designation list") so nothing is silently included.

Loaded from the mutable reference bucket in S3; absent/unreadable ⇒ an empty list
(rule-based backstop only, i.e. the pre-list behavior).
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any

from openpyxl import load_workbook

from backend.lambdas.aml_export import quality
from services.logging import get_logger

logger = get_logger(__name__)

_MATTER_HEADERS = {"matter number", "matter", "matter #", "matter no"}
_DESIGNATION_HEADERS = {
    "designation",
    "include/exclude",
    "include or exclude",
    "include exclude",
    "action",
}
_PARTY_HEADERS = {"party name", "full legal name", "name", "party"}
_REASON_HEADERS = {"reason", "notes", "comment"}


@dataclass
class Designations:
    """Parsed designation list. Empty instance ⇒ backstop-only behavior."""

    matters: dict[str, tuple[str, str]] = field(
        default_factory=dict
    )  # key -> (include|exclude, reason)
    parties: dict[str, str] = field(default_factory=dict)  # normalized name -> reason

    @property
    def has_matters(self) -> bool:
        return bool(self.matters)

    def matter_decision(self, number: Any) -> tuple[str | None, str | None]:
        dec = self.matters.get(quality.normalize_text(number).lower())
        return (dec[0], dec[1]) if dec else (None, None)

    def covers_matter(self, number: Any) -> bool:
        key = quality.normalize_text(number).lower()
        return bool(key) and key in self.matters

    def party_excluded(self, name: Any) -> str | None:
        return self.parties.get(quality.normalize_text(name).lower())


def _col(headers: list[str], names: set[str]) -> int | None:
    for i, h in enumerate(headers):
        if str(h or "").strip().lower() in names:
            return i
    return None


def _cell(row: tuple, idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    return quality.normalize_text(row[idx])


def parse_designations(wb: Any) -> Designations:
    """Parse the workbook into a :class:`Designations` (tolerant of layout)."""
    d = Designations()
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            continue
        headers = [str(c or "").strip() for c in rows[0]]
        m_i = _col(headers, _MATTER_HEADERS)
        des_i = _col(headers, _DESIGNATION_HEADERS)
        p_i = _col(headers, _PARTY_HEADERS)
        r_i = _col(headers, _REASON_HEADERS)

        if m_i is not None and des_i is not None:
            for row in rows[1:]:
                number = _cell(row, m_i).lower()
                designation = _cell(row, des_i).lower()
                reason = _cell(row, r_i)
                if number and designation in ("include", "exclude"):
                    d.matters[number] = (designation, reason)
        elif p_i is not None:
            for row in rows[1:]:
                name = _cell(row, p_i).lower()
                if name:
                    d.parties[name] = _cell(row, r_i) or "Excluded by Aphrodite designation list"
    return d


def load_designations(s3: Any | None, bucket: str | None, key: str | None) -> Designations:
    """Load the designation list from S3; absent/unreadable ⇒ empty (backstop-only)."""
    if not (s3 and bucket and key):
        return Designations()
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        wb = load_workbook(io.BytesIO(obj["Body"].read()), read_only=True, data_only=True)
        parsed = parse_designations(wb)
        logger.info(
            "designation list loaded",
            extra={
                "extra_fields": {
                    "matters": len(parsed.matters),
                    "party_excludes": len(parsed.parties),
                }
            },
        )
        return parsed
    except Exception:
        logger.warning(
            "designation list unavailable; using rule-based backstop only",
            extra={"extra_fields": {"key": key}},
        )
        return Designations()
