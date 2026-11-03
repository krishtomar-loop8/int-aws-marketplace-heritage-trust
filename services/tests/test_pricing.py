"""Unit tests for per-search Bedrock cost accounting."""

from __future__ import annotations

from services import pricing


def test_lookup_resolves_cross_region_profile() -> None:
    # The us.* inference-profile prefix must resolve to the base family price.
    price = pricing.lookup("us.anthropic.claude-sonnet-4-6")
    assert price is not None
    assert price.input_per_1m == 3.0
    assert price.output_per_1m == 15.0


def test_lookup_unknown_model_returns_none() -> None:
    assert pricing.lookup("some.unknown-model") is None


def test_cost_usd_sums_all_token_buckets() -> None:
    # 1M input @ $3 + 1M output @ $15 + 1M cache-read @ $0.30 + 1M cache-write @ $3.75.
    cost = pricing.cost_usd(
        "us.anthropic.claude-sonnet-4-6",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
    )
    assert cost == 3.0 + 15.0 + 0.30 + 3.75


def test_cost_usd_realistic_search() -> None:
    # ~7k output tokens dominate; a small uncached input + a cached framework read.
    cost = pricing.cost_usd(
        "us.anthropic.claude-sonnet-4-6",
        input_tokens=2_000,
        output_tokens=7_000,
        cache_read_tokens=54_000,
    )
    # 2000*3 + 7000*15 + 54000*0.30 all / 1e6
    assert cost == round((2_000 * 3.0 + 7_000 * 15.0 + 54_000 * 0.30) / 1_000_000, 6)


def test_cost_usd_unpriced_model_is_none() -> None:
    assert pricing.cost_usd("test-model", 100, 40) is None


def test_haiku_is_cheaper_than_opus_for_same_usage() -> None:
    usage = dict(input_tokens=2_000, output_tokens=7_000, cache_read_tokens=54_000)
    haiku = pricing.cost_usd("us.anthropic.claude-haiku-4-5", **usage)
    opus = pricing.cost_usd("us.anthropic.claude-opus-4-8", **usage)
    assert haiku is not None and opus is not None
    assert haiku < opus
