"""Anthropic model ids and prices, in one place.

UPDATE WHEN PRICES CHANGE. Verify against https://platform.claude.com/docs/en/pricing
before editing — these numbers are copied by hand and nothing checks them at runtime.
Verified 2026-08-01 against the claude-api skill reference.

Alias policy: `sonnet` pins claude-sonnet-4-6, not claude-sonnet-5 — sonnet-5 carries
a new tokenizer (~30% more tokens for the same text), so the current measured cost
model would silently shift. Re-evaluate after the sonnet-5 intro window (2026-08-31).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backglass.extract.client import ModelError

MODEL_IDS: dict[str, str] = {
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-4-6",
}


@dataclass(frozen=True)
class Price:
    """$ per million tokens."""

    input: float
    output: float
    cache_read: float      # 0.1x input
    cache_write_5m: float  # 1.25x input (5-minute TTL)


PRICES: dict[str, Price] = {
    "claude-haiku-4-5": Price(input=1.00, output=5.00, cache_read=0.10, cache_write_5m=1.25),
    "claude-sonnet-4-6": Price(input=3.00, output=15.00, cache_read=0.30, cache_write_5m=3.75),
}


def resolve(alias_or_id: str) -> str:
    """'haiku' → concrete id; a concrete id passes through unchanged."""
    return MODEL_IDS.get(alias_or_id, alias_or_id)


def cost_usd(model_id: str, usage: Any) -> float:
    """Actual cost from a response's usage token counts.

    An unpriced model raises rather than pricing at $0 — the spend cap is enforced,
    not monitored (CLAUDE.md rule 7), and a silent zero would quietly disable it.
    """
    price = PRICES.get(model_id)
    if price is None:
        raise ModelError(
            f"no price on record for model {model_id!r}; add it to extract/pricing.py"
        )
    return (
        getattr(usage, "input_tokens", 0) * price.input
        + getattr(usage, "output_tokens", 0) * price.output
        + (getattr(usage, "cache_read_input_tokens", 0) or 0) * price.cache_read
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * price.cache_write_5m
    ) / 1_000_000
