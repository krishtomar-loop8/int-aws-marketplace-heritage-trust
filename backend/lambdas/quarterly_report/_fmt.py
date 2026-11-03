"""Presentation helpers shared by the docx and pdf renderers."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

# Shown in place of a figure that is a client follow-up (the investment total).
PENDING = "[to be confirmed]"


def money(value: Decimal | None, *, pending: str = PENDING) -> str:
    """Format a money value as ``$1,234.56``; ``None`` ⇒ the pending marker."""
    if value is None:
        return pending
    return f"${value:,.2f}"


def money_or_dash(value: Decimal | None) -> str:
    """Format a money value, using an en-dash for an absent/zero cell (ledger)."""
    if value is None:
        return "-"
    return f"${value:,.2f}"


def long_date(day: date) -> str:
    """Format a date the way the sample letter does, e.g. ``May 11, 2026``."""
    return f"{day.strftime('%B')} {day.day}, {day.year}"
