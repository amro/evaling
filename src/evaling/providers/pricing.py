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


#: Output tokens assumed for a model that sets no ``max_tokens``. Only used to
#: put an estimate in the right order of magnitude; the caller says so.
ASSUMED_OUTPUT_TOKENS = 500


@dataclass(frozen=True)
class CostEstimate:
    """Roughly what a run will cost, before it runs.

    An estimate and not a bound. Input tokens are approximated from character
    counts rather than a real tokenizer, output length is capped by
    ``max_tokens`` only where a model sets one, the price table is a
    convenience rather than an invoice, and retried calls bill again. Every
    one of those moves the real figure, so callers should present it as an
    estimate.

    ``priced`` is False when some model has no pricing, in which case ``usd``
    covers only the models that do and ``unpriced`` names the rest.
    """

    usd: float
    priced: bool
    unpriced: tuple[str, ...] = ()


def estimate_run(groups: list[tuple[str, dict[str, Any], int, int]]) -> CostEstimate | None:
    """Estimate a run from ``(model_id, params, input_tokens_each, cells)`` groups.

    Both sides scale with the cell count — the output half is what a run
    actually spends most on, and leaving it per-cell made a 20-case estimate
    indistinguishable from a 2-case one.

    Returns None when nothing could be priced at all, so a caller can say
    "unknown" rather than "$0.00", which would read as free.
    """
    total = 0.0
    unpriced: list[str] = []
    priced_any = False
    for model_id, params, input_tokens_each, cells in groups:
        max_tokens = (params or {}).get("max_tokens")
        if isinstance(max_tokens, int) and max_tokens > 0:
            output_each = max_tokens
        else:
            output_each = ASSUMED_OUTPUT_TOKENS
        cost = estimate_cost(
            str((params or {}).get("model", model_id)),
            input_tokens_each * cells,
            output_each * cells,
            params,
        )
        if cost is None:
            if model_id not in unpriced:
                unpriced.append(model_id)
            continue
        priced_any = True
        total += cost
    if not priced_any:
        return None
    return CostEstimate(round(total, 4), not unpriced, tuple(unpriced))
