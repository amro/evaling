"""Token pricing for cost estimates.

Rates are USD per million tokens. The built-in table covers the published
first-party rates for Anthropic, OpenAI, and Google Gemini models. Anything
else reports no cost unless the model config supplies its own ``pricing`` —
notably a self-hosted or brokered endpoint (Ollama, vLLM, OpenRouter, an
internal gateway), where the operator sets the rates and no table can know
them:

    models:
      - id: gpt-5.2
        provider: openai
        params:
          pricing: {input: 1.25, output: 10.0}   # USD per million tokens

Published prices change. Treat built-in numbers as a convenience, not an
invoice, and override them in config when accuracy matters.

The table is the standard first-party rate: not batch (half), not cached
input (a tenth), not fast mode, and not the regional multipliers or the
partner platforms' own rates. Anything paying one of those should say so with
``params.pricing`` rather than expect the table to guess which.

Lookup is by model id alone, so an endpoint serving something under a
published id — a local model you named ``gemini-2.5-pro``, a broker
reselling one — is priced as though it were that model. Set ``pricing`` on
those, and see ``docs/providers.md``.
"""

from dataclasses import dataclass
from typing import Any

#: When the built-in table was last checked against published pricing.
PRICING_AS_OF = "2026-08-17"


@dataclass(frozen=True)
class Price:
    """USD per million tokens."""

    input: float
    output: float


# Published first-party rate cards, standard (non-batch, non-cached) rates in
# USD per million tokens. Mirroring the published tables rather than curating
# them keeps "is this current?" a question anyone can answer by comparing two
# lists; RELEASING.md step 1 is where that comparison happens.
#
# Text models only, because those are the ones evaling can call. Base
# completion models (davinci-002, babbage-002, gpt-3.5-turbo-instruct) and the
# image, video, speech, and embedding models are left out for the same reason.
#
# Deprecated and retired models are kept where they are still reachable — the
# Claude ones remain callable on the cloud platforms — because a model that
# vanishes from this table starts reporting an *unknown* cost, which is a
# worse answer than a slightly stale one. Models that have actually shut down
# are dropped, since nothing can call them.
PRICES: dict[str, Price] = {
    # -- Anthropic --------------------------------------------------------
    # Cloud platforms bill their own rates; the figure here is first-party.
    "claude-fable-5": Price(10.00, 50.00),
    "claude-mythos-5": Price(10.00, 50.00),
    "claude-opus-5": Price(5.00, 25.00),
    "claude-opus-4-8": Price(5.00, 25.00),
    "claude-opus-4-7": Price(5.00, 25.00),
    "claude-opus-4-6": Price(5.00, 25.00),
    "claude-opus-4-5": Price(5.00, 25.00),
    "claude-opus-4-1": Price(15.00, 75.00),
    "claude-opus-4": Price(15.00, 75.00),
    # $2/$10 launched as introductory pricing through 2026-08-31; Anthropic has
    # since made it the standard rate and cancelled the rise to $3/$15. This
    # table carried the $3/$15 until 2026-08-17 on the assumption the increase
    # would happen, which read 50% high.
    "claude-sonnet-5": Price(2.00, 10.00),
    "claude-sonnet-4-6": Price(3.00, 15.00),
    "claude-sonnet-4-5": Price(3.00, 15.00),
    "claude-sonnet-4": Price(3.00, 15.00),
    "claude-haiku-4-5": Price(1.00, 5.00),
    "claude-haiku-3-5": Price(0.80, 4.00),
    # -- OpenAI -----------------------------------------------------------
    "gpt-5.6-sol": Price(5.00, 30.00),
    "gpt-5.6-terra": Price(2.00, 12.00),
    "gpt-5.6-luna": Price(0.20, 1.20),
    "gpt-5.5": Price(5.00, 30.00),
    "gpt-5.5-pro": Price(30.00, 180.00),
    "gpt-5.4": Price(2.50, 15.00),
    "gpt-5.4-mini": Price(0.75, 4.50),
    "gpt-5.4-nano": Price(0.20, 1.25),
    "gpt-5.4-pro": Price(30.00, 180.00),
    # 5.3 ships only as these two; there is no plain `gpt-5.3` on the card.
    # Lookup is by exact id, so a variant priced the same as its base still
    # has to be listed or it reports an unknown cost.
    "gpt-5.3-chat-latest": Price(1.75, 14.00),
    "gpt-5.3-codex": Price(1.75, 14.00),
    # Unversioned alias for the current flagship chat model, and priced as one
    # rather than as the 5.2/5.3 entries above.
    "chat-latest": Price(5.00, 30.00),
    "gpt-5.2": Price(1.75, 14.00),
    "gpt-5.2-pro": Price(21.00, 168.00),
    "gpt-5.2-chat-latest": Price(1.75, 14.00),
    "gpt-5.1": Price(1.25, 10.00),
    "gpt-5": Price(1.25, 10.00),
    "gpt-5-mini": Price(0.25, 2.00),
    "gpt-5-nano": Price(0.05, 0.40),
    "gpt-5-pro": Price(15.00, 120.00),
    "gpt-5-search-api": Price(1.25, 10.00),
    "gpt-4.1": Price(2.00, 8.00),
    "gpt-4.1-mini": Price(0.40, 1.60),
    "gpt-4.1-nano": Price(0.10, 0.40),
    "gpt-4o": Price(2.50, 10.00),
    # Priced above its own base id, so it is listed rather than assumed.
    "gpt-4o-2024-05-13": Price(5.00, 15.00),
    "gpt-4o-mini": Price(0.15, 0.60),
    "gpt-4-turbo-2024-04-09": Price(10.00, 30.00),
    "gpt-4-0613": Price(30.00, 60.00),
    "gpt-3.5-turbo": Price(0.50, 1.50),
    "gpt-3.5-turbo-0125": Price(0.50, 1.50),
    "gpt-3.5-turbo-1106": Price(1.00, 2.00),
    "o1": Price(15.00, 60.00),
    "o1-pro": Price(150.00, 600.00),
    "o3": Price(2.00, 8.00),
    "o3-pro": Price(20.00, 80.00),
    "o3-mini": Price(1.10, 4.40),
    "o4-mini": Price(1.10, 4.40),
    # -- Google Gemini ----------------------------------------------------
    # Reached through the `openai-compatible` provider, which prices by model
    # id like everything else here.
    #
    # Two Pro models are tiered by prompt length. The figure below is the
    # short-prompt tier, which every ordinary eval falls in: doubling the
    # estimate for a threshold almost nobody crosses would make the number
    # useless for the runs people actually have. Past ~200k input tokens per
    # call, set `params.pricing` to the long-prompt tier.
    # 3.7 and 3.6 Flash are both promoted to $0.75/$3.75 through 2026-12-31,
    # reverting to the standard rate below. The table carries standard rates,
    # so these read high while the promotion runs rather than low after it.
    "gemini-3.7-flash": Price(1.50, 7.50),
    "gemini-3.6-flash": Price(1.50, 7.50),
    "gemini-3.5-flash": Price(1.50, 9.00),
    "gemini-3.5-flash-lite": Price(0.30, 2.50),
    # Text input; audio in costs $0.50.
    "gemini-3.1-flash-lite": Price(0.25, 1.50),
    "gemini-3.1-pro-preview": Price(2.00, 12.00),  # >200k: $4.00 / $18.00
    # Text input; audio in costs $1.00.
    "gemini-3-flash-preview": Price(0.50, 3.00),
    # The 2.5 models are closed to new Gemini API users (a call returns 404
    # naming that), and kept for the same reason as Anthropic's retired ones:
    # accounts that already had access can still call them, and a model missing
    # from the table reports an unknown cost rather than a correct one.
    "gemini-2.5-pro": Price(1.25, 10.00),  # >200k: $2.50 / $15.00
    "gemini-2.5-flash": Price(0.30, 2.50),
    "gemini-2.5-flash-lite": Price(0.10, 0.40),
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
