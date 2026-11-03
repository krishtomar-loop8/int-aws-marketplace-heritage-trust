"""Per-search cost accounting for Bedrock synthesis.

Bedrock bills the Converse API by token: input, output, and — when a
``cachePoint`` is used (our static framework prefix, see ``services.llm``) —
cache-read and cache-write tokens at separate rates. This module maps a model id
to its published on-demand Bedrock prices and computes the dollar cost of a
single synthesis call, so the matter-search audit can report cost-per-search and
the client cost comparison can be assembled from real usage.

Prices are USD per 1,000,000 tokens, from the AWS Bedrock pricing page
(https://aws.amazon.com/bedrock/pricing/) cross-checked against Anthropic's
published rates, verified 2026-07-14. Cache-read is ~10% of the input rate and
5-minute cache-write ~125%, per Anthropic's prompt-caching model.

Caveats to keep in mind when quoting absolute figures to the client:
- These models run via cross-region ``us.*`` inference profiles. AWS bills
  cross-region inference at the source-region on-demand rate used here; some
  third-party trackers report a ~10% cross-region premium. Confirm against the
  live pricing page before publishing final numbers.
- Claude Sonnet 5 is on a promotional launch price of $2/$10 (in/out) through
  2026-08-31; we price it at the durable **standard** $3/$15 so a monthly
  projection is not understated once the promo ends (flag the current discount
  separately in the client deliverable).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPrice:
    """On-demand Bedrock price, USD per 1,000,000 tokens."""

    input_per_1m: float
    output_per_1m: float
    cache_read_per_1m: float
    cache_write_per_1m: float


# Keyed by a model-family substring matched against the normalized model id, so a
# region-prefixed / date-versioned inference profile (e.g.
# ``us.anthropic.claude-sonnet-4-6``) resolves to the right price. Order does not
# matter — the family tokens are mutually exclusive.
_PRICING: dict[str, ModelPrice] = {
    "claude-opus-4-8": ModelPrice(6.0, 30.0, 0.60, 7.50),
    "claude-sonnet-5": ModelPrice(3.0, 15.0, 0.30, 3.75),  # promo $2/$10 until 2026-08-31
    "claude-sonnet-4-6": ModelPrice(3.0, 15.0, 0.30, 3.75),  # current prod baseline
    "claude-sonnet-4-5": ModelPrice(3.0, 15.0, 0.30, 3.75),
    "claude-haiku-4-5": ModelPrice(1.0, 5.0, 0.10, 1.25),
    # In-region on-demand fallback documented in services.llm (Claude 3 Sonnet).
    "claude-3-sonnet": ModelPrice(3.0, 15.0, 0.30, 3.75),
}

_REGION_PREFIXES = ("us.", "global.", "eu.", "apac.", "ca.")


def _normalize(model_id: str) -> str:
    """Strip the cross-region routing prefix so a profile id matches a family key."""
    mid = model_id.strip().lower()
    for prefix in _REGION_PREFIXES:
        if mid.startswith(prefix):
            return mid[len(prefix) :]
    return mid


def lookup(model_id: str) -> ModelPrice | None:
    """Return the price for a model id, or None if it is not in the table."""
    mid = _normalize(model_id)
    for family, price in _PRICING.items():
        if family in mid:
            return price
    return None


def cost_usd(
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float | None:
    """Dollar cost of one synthesis call, or ``None`` if the model is unpriced.

    Bedrock's ``usage.inputTokens`` reports the **uncached** input tokens; cache
    reads and writes are reported separately (``cacheReadInputTokens`` /
    ``cacheWriteInputTokens``), so the four token buckets are summed at their own
    rates with no double-counting.
    """
    price = lookup(model_id)
    if price is None:
        return None
    cost = (
        input_tokens * price.input_per_1m
        + output_tokens * price.output_per_1m
        + cache_read_tokens * price.cache_read_per_1m
        + cache_write_tokens * price.cache_write_per_1m
    ) / 1_000_000
    return round(cost, 6)
