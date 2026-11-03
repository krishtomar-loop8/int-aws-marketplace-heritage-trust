"""Unit tests for the reporting-period date helpers."""

from __future__ import annotations

from datetime import date

import pytest

from backend.lambdas.quarterly_report.period import ReportingPeriod, previous_quarter


def test_quarter_bounds_and_labels():
    q1 = ReportingPeriod(2026, 1)
    assert q1.start == date(2026, 1, 1)
    assert q1.end == date(2026, 3, 31)
    assert q1.label == "First Quarter"
    assert q1.code == "2026-Q1"

    q4 = ReportingPeriod(2026, 4)
    assert q4.start == date(2026, 10, 1)
    assert q4.end == date(2026, 12, 31)
    assert q4.label == "Fourth Quarter"


def test_contains_is_inclusive():
    q1 = ReportingPeriod(2026, 1)
    assert q1.contains(date(2026, 1, 1))
    assert q1.contains(date(2026, 3, 31))
    assert not q1.contains(date(2026, 4, 1))
    assert not q1.contains(date(2025, 12, 31))


def test_invalid_quarter_rejected():
    with pytest.raises(ValueError):
        ReportingPeriod(2026, 5)


def test_previous_quarter_within_year():
    # A run any time in Q2 reports on the just-finished Q1.
    assert previous_quarter(date(2026, 5, 11)) == ReportingPeriod(2026, 1)
    assert previous_quarter(date(2026, 9, 30)) == ReportingPeriod(2026, 2)


def test_previous_quarter_wraps_year():
    # January runs report on the prior year's Q4.
    assert previous_quarter(date(2026, 1, 15)) == ReportingPeriod(2025, 4)
