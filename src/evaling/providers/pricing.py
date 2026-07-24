"""Token pricing for cost estimates.

Rates are USD per million tokens. The built-in table covers models whose
published pricing we track; anything else (including every OpenAI-compatible
endpoint, where the operator sets their own rates) reports no cost unless the
model config supplies its own ``pricing``:

    models:
      - id: gpt-5.2
        provider: openai
        params:
          pricing: {input: 1.25, output: 10.0}   # USD per million tokens

Published prices change. Treat built-in numbers as a convenience, not an
invoice, and override them in config when accuracy matters.
"""

from dataclasses import dataclass
from typing import Any

#: When the built-in table was last checked against published pricing.
PRICING_AS_OF = "2026-06-24"


@dataclass(frozen=True)
class Price:
    """USD per million tokens."""

    input: float
    output: float


# Anthropic published rates (USD per million tokens).
PRICES: dict[str, Price] = {
    "claude-fable-5": Price(10.00, 50.00),
    "claude-mythos-5": Price(10.00, 50.00),
    "claude-opus-5": Price(5.00, 25.00),
    "claude-opus-4-8": Price(5.00, 25.00),
    "claude-opus-4-7": Price(5.00, 25.00),
    "claude-opus-4-6": Price(5.00, 25.00),
    "claude-sonnet-5": Price(3.00, 15.00),
    "claude-sonnet-4-6": Price(3.00, 15.00),
    "claude-haiku-4-5": Price(1.00, 5.00),
}


def price_for(model: str, params: dict[str, Any] | None = None) -> Price | None:
    """Resolve pricing for a model: config override first, then the table.

    The config schema validates overrides at load time; this stays defensive
    anyway, because a pricing problem must never destroy a response the user
    has already paid for.
    """
    override = (params or {}).get("pricing")
    if isinstance(override, dict) and "input" in override and "output" in override:
        try:
            price = Price(float(override["input"]), float(override["output"]))
        except (TypeError, ValueError):
            return PRICES.get(model)
        if price.input >= 0 and price.output >= 0:
            return price
        return PRICES.get(model)
    return PRICES.get(model)


def estimate_cost(
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
    params: dict[str, Any] | None = None,
) -> float | None:
    """Cost in USD, or None when pricing or usage is unknown.

    A priced model that reports no usage at all yields None, not 0.0 — an
    endpoint that omits usage means the cost is unknown, and reporting $0
    would be a guess that silently under-counts spend.
    """
    price = price_for(model, params)
    if price is None:
        return None
    if input_tokens is None and output_tokens is None:
        return None
    cost = ((input_tokens or 0) * price.input + (output_tokens or 0) * price.output) / 1_000_000
    return round(cost, 8)
