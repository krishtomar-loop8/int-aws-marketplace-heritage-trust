"""Tests for Tier-0 intent classification."""

from __future__ import annotations

import pytest

from backend.search.intent import classify


@pytest.mark.parametrize(
    "query,expected",
    [
        ("what's the trust balance on the Lee estate?", "balance"),
        ("how much is in the McDonald trust account?", "balance"),
        ("is KYC complete for the Smith beneficiaries?", "kyc"),
        ("upcoming deadlines for the Jones matter", "deadlines"),
        ("who are the beneficiaries on the Sparks estate?", "parties"),
        ("what documents are on file for Brown?", "documents"),
        ("show me recent emails on the Lee matter", "correspondence"),
        ("what's the T3 filing status for McDonald?", "tax_status"),
        ("any outstanding invoices on the Jones file?", "bills"),
        ("what's the date of birth of Oonagh McDonald?", "profile"),
        ("when did Oonagh McDonald die?", "profile"),
        ("date of death for the Smith estate", "profile"),
        ("what's the marital status of the deceased?", "profile"),
        ("what province is the Jones matter in?", "profile"),
    ],
)
def test_narrow_intents_classify(query: str, expected: str) -> None:
    assert classify(query) == expected


@pytest.mark.parametrize(
    "query",
    [
        "what's the status of Oonagh McDonald?",
        "give me a summary of the Lee estate",
        "status on the Smith matter",
        "tell me about the Jones trust",
        "is the Lee matter ready for distribution?",
        "where are things with the Brown estate?",
        "catch me up on McDonald",
    ],
)
def test_overview_queries_fall_through_to_synthesis(query: str) -> None:
    assert classify(query) is None


def test_overview_word_beats_narrow_keyword() -> None:
    # A comprehensive ask wins even when a narrow keyword is present.
    assert classify("full status including the trust balance for Lee") is None


def test_unmatched_returns_none() -> None:
    assert classify("what colour is the sky?") is None
    assert classify("") is None
