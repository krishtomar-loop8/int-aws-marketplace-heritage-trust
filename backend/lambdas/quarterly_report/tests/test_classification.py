"""Unit tests for the quarterly-report account classifier. No AWS/network.

The classifier turns a Clio trust-account *name* into an asset category for the
itemized "Assets Held" breakdown, per Nicole's rules (2026-07-21). An account it
cannot confidently place is still counted as Cash but returned ``confident=False``
so the draft flags it rather than bucketing it silently.
"""

from __future__ import annotations

import io

from openpyxl import Workbook

from backend.lambdas.quarterly_report.classification import (
    DEFAULT_CLASSIFIER,
    load_classification,
    parse_classification,
)


def test_default_rules_classify_nicoles_examples():
    c = DEFAULT_CLASSIFIER
    assert c.classify("Assets Under Administration - Real Property") == ("Real Property", True)
    assert c.classify("GICs Held in Trust at RBC") == ("GICs", True)
    assert c.classify("External Bank/Investment Account") == ("Investments", True)
    assert c.classify("Aphrodite Main Trust Account") == ("Cash", True)


def test_gic_takes_priority_over_investment_and_cash_tokens():
    # Contains "investment account" and "trust", but "gic" is checked first.
    assert DEFAULT_CLASSIFIER.classify("GIC Investment Account held in Trust") == ("GICs", True)


def test_unknown_account_is_cash_but_not_confident():
    category, confident = DEFAULT_CLASSIFIER.classify("Special Holding 123")
    assert category == "Cash"
    assert confident is False


def test_blank_name_is_unconfident_cash():
    assert DEFAULT_CLASSIFIER.classify("") == ("Cash", False)
    assert DEFAULT_CLASSIFIER.classify(None) == ("Cash", False)


def test_matching_is_case_and_whitespace_insensitive():
    assert DEFAULT_CLASSIFIER.classify("  ASSETS -  REAL   PROPERTY ") == ("Real Property", True)


def _wb(rows: list[list[str]]) -> Workbook:
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    return wb


def _wb_bytes(rows: list[list[str]]) -> bytes:
    buffer = io.BytesIO()
    _wb(rows).save(buffer)
    return buffer.getvalue()


def test_parse_classification_extends_defaults():
    wb = _wb(
        [
            ["Category", "Pattern"],
            ["Investments", "RBC Dominion Securities"],  # a named custodian
            ["Cash", "Concentration"],  # a novel cash pattern
        ]
    )
    c = parse_classification(wb)
    assert c.classify("RBC Dominion Securities Portfolio") == ("Investments", True)
    assert c.classify("Concentration Account") == ("Cash", True)
    # Bundled defaults still apply.
    assert c.classify("Assets - Real Property") == ("Real Property", True)


def test_load_classification_absent_returns_defaults():
    assert load_classification(None, None, None) is DEFAULT_CLASSIFIER
    assert load_classification(object(), "bucket", None) is DEFAULT_CLASSIFIER


class _FakeS3:
    def __init__(self, body: bytes | Exception) -> None:
        self._body = body

    def get_object(self, Bucket, Key):  # noqa: N803 - boto3 kwarg names
        if isinstance(self._body, Exception):
            raise self._body
        return {"Body": io.BytesIO(self._body)}


def test_load_classification_reads_workbook_from_s3():
    body = _wb_bytes([["Category", "Pattern"], ["GICs", "Term Deposit"]])
    c = load_classification(_FakeS3(body), "bucket", "config/account_classification.xlsx")
    assert c.classify("Term Deposit Ladder") == ("GICs", True)


def test_load_classification_unreadable_falls_back_to_defaults():
    c = load_classification(_FakeS3(RuntimeError("boom")), "bucket", "key")
    assert c is DEFAULT_CLASSIFIER
