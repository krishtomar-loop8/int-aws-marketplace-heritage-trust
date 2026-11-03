"""Reporting-period helpers for the quarterly trust statement.

Pure date arithmetic — no I/O — so the generator and handler stay testable. A
:class:`ReportingPeriod` names a calendar quarter (Jan–Mar = Q1, … Oct–Dec = Q4)
and carries the inclusive ``start``/``end`` dates plus the human label used in the
cover letter ("First Quarter").
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

# Cover-letter wording for each quarter, e.g. "First Quarter Trust Statement".
_QUARTER_LABELS = {1: "First Quarter", 2: "Second Quarter", 3: "Third Quarter", 4: "Fourth Quarter"}

# Inclusive (start_month, end_month) for each quarter.
_QUARTER_MONTHS = {1: (1, 3), 2: (4, 6), 3: (7, 9), 4: (10, 12)}


def _end_of_month(year: int, month: int) -> date:
    """Last calendar day of ``month`` in ``year``."""
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


@dataclass(frozen=True)
class ReportingPeriod:
    """A calendar quarter to report on."""

    year: int
    quarter: int  # 1–4

    def __post_init__(self) -> None:
        if self.quarter not in _QUARTER_MONTHS:
            raise ValueError(f"quarter must be 1–4, got {self.quarter}")

    @property
    def start(self) -> date:
        start_month = _QUARTER_MONTHS[self.quarter][0]
        return date(self.year, start_month, 1)

    @property
    def end(self) -> date:
        end_month = _QUARTER_MONTHS[self.quarter][1]
        return _end_of_month(self.year, end_month)

    @property
    def label(self) -> str:
        """Cover-letter wording, e.g. "First Quarter"."""
        return _QUARTER_LABELS[self.quarter]

    @property
    def code(self) -> str:
        """Filesystem/S3-safe period code, e.g. "2026-Q1"."""
        return f"{self.year}-Q{self.quarter}"

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end


def previous_quarter(today: date) -> ReportingPeriod:
    """The most recently *completed* calendar quarter before ``today``.

    Quarterly statements report on a finished quarter, so the default period is
    the one ending before the current quarter (e.g. a run any time in Q2 reports
    on Q1).
    """
    current_q = (today.month - 1) // 3 + 1
    if current_q == 1:
        return ReportingPeriod(today.year - 1, 4)
    return ReportingPeriod(today.year, current_q - 1)
