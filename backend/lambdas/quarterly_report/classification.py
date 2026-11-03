"""Classify a matter's Clio trust accounts into asset categories.

Aphrodite records some assets (real property, external investment accounts,
GICs) directly in Clio as specially-named trust "bank accounts" — most visibly on
committeeship matters. Left unclassified those get summed into "Cash Accounts",
mislabelling real property / investments / GICs as cash (README Open Items #1/#8).

Nicole's classification rules (2026-07-21), matched as case-insensitive substrings
("any account name containing …"):

  * Real Property — contains "Real Property"
  * GICs          — contains "GIC"
  * Investments   — contains "External Bank" or "Investment Account" (or a named
                    investment counsel / custodian)
  * Cash          — everything else

An account matching none of the known patterns is still counted as **Cash** (so the
totals reconcile) but returned as ``confident=False`` so the draft **flags** it for
staff rather than bucketing it silently — the same "kept but flagged" principle the
AML designation list uses (:mod:`backend.lambdas.aml_export.designations`).

The default rules ship in-code so the generator works out of the box; an optional
``config/account_classification.xlsx`` in the mutable reference bucket (two columns,
``Category`` | ``Pattern``) lets staff add patterns with **no redeploy** — mirrors
:func:`backend.lambdas.aml_export.designations.load_designations`.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any

from openpyxl import load_workbook

from backend.lambdas.aml_export import quality
from services.logging import get_logger

logger = get_logger(__name__)

# Canonical category labels (also the row descriptions in the Assets Held table).
CATEGORY_REAL_PROPERTY = "Real Property"
CATEGORY_INVESTMENTS = "Investments"
CATEGORY_GICS = "GICs"
CATEGORY_CASH = "Cash"

# Bundled defaults = Nicole's rules. Real Property / GICs are tested before
# Investments so a name like "GIC Investment Account" lands in GICs (the more
# specific instrument). The Cash patterns are known cash-account names from live
# data, used *only* to decide whether an unmatched account is confidently cash.
_DEFAULT_PATTERNS: dict[str, tuple[str, ...]] = {
    CATEGORY_REAL_PROPERTY: ("real property",),
    CATEGORY_GICS: ("gic",),
    CATEGORY_INVESTMENTS: ("external bank", "investment account"),
    CATEGORY_CASH: ("trust", "main", "pooled", "chequing", "cheque", "cash"),
}

# Non-cash categories, in match-priority order (first hit wins). Cash is the
# catch-all and is only consulted for the confidence check.
_MATCH_ORDER = (CATEGORY_REAL_PROPERTY, CATEGORY_GICS, CATEGORY_INVESTMENTS)


@dataclass
class AccountClassifier:
    """Maps a Clio account name to an asset category via substring patterns."""

    patterns: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(_DEFAULT_PATTERNS)
    )

    def classify(self, account_name: Any) -> tuple[str, bool]:
        """Return ``(category, confident)`` for an account name.

        A name matching a Real Property / GICs / Investments pattern → that category
        (confident). Otherwise it is **Cash**: confident when it matches a known cash
        pattern, else ``confident=False`` so the draft flags it for staff.
        """
        text = quality.normalize_text(account_name).lower()
        if not text:
            return CATEGORY_CASH, False
        for category in _MATCH_ORDER:
            if any(pattern in text for pattern in self.patterns.get(category, ())):
                return category, True
        if any(pattern in text for pattern in self.patterns.get(CATEGORY_CASH, ())):
            return CATEGORY_CASH, True
        return CATEGORY_CASH, False


DEFAULT_CLASSIFIER = AccountClassifier()


# -- optional config sheet (reference bucket) --------------------------------

_CATEGORY_HEADERS = {"category", "asset category", "class"}
_PATTERN_HEADERS = {"pattern", "account name pattern", "contains", "name contains", "keyword"}
# Free-text category labels the sheet may use → canonical category names.
_CATEGORY_ALIASES = {
    "real property": CATEGORY_REAL_PROPERTY,
    "real estate": CATEGORY_REAL_PROPERTY,
    "property": CATEGORY_REAL_PROPERTY,
    "investments": CATEGORY_INVESTMENTS,
    "investment": CATEGORY_INVESTMENTS,
    "gics": CATEGORY_GICS,
    "gic": CATEGORY_GICS,
    "cash": CATEGORY_CASH,
}


def _canonical_category(label: str) -> str | None:
    return _CATEGORY_ALIASES.get(quality.normalize_text(label).lower())


def _col(headers: list[str], names: set[str]) -> int | None:
    for i, header in enumerate(headers):
        if str(header or "").strip().lower() in names:
            return i
    return None


def _cell(row: tuple, idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    return quality.normalize_text(row[idx])


def parse_classification(wb: Any) -> AccountClassifier:
    """Parse a ``Category`` | ``Pattern`` workbook, extending the in-code defaults."""
    patterns: dict[str, list[str]] = {k: list(v) for k, v in _DEFAULT_PATTERNS.items()}
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            continue
        headers = [str(c or "").strip() for c in rows[0]]
        c_i = _col(headers, _CATEGORY_HEADERS)
        p_i = _col(headers, _PATTERN_HEADERS)
        if c_i is None or p_i is None:
            continue
        for row in rows[1:]:
            category = _canonical_category(_cell(row, c_i))
            pattern = _cell(row, p_i).lower()
            if category and pattern and pattern not in patterns[category]:
                patterns[category].append(pattern)
    return AccountClassifier(patterns={k: tuple(v) for k, v in patterns.items()})


def load_classification(s3: Any | None, bucket: str | None, key: str | None) -> AccountClassifier:
    """Load the classification config from S3; absent/unreadable ⇒ in-code defaults."""
    if not (s3 and bucket and key):
        return DEFAULT_CLASSIFIER
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        wb = load_workbook(io.BytesIO(obj["Body"].read()), read_only=True, data_only=True)
        classifier = parse_classification(wb)
        logger.info(
            "account classification config loaded",
            extra={
                "extra_fields": {
                    "patterns": {k: len(v) for k, v in classifier.patterns.items()}
                }
            },
        )
        return classifier
    except Exception:  # noqa: BLE001 - config is optional; never fail the run on it
        logger.warning(
            "account classification config unavailable; using in-code defaults",
            extra={"extra_fields": {"key": key}},
        )
        return DEFAULT_CLASSIFIER
