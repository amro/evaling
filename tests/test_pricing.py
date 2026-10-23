import re

import pytest

from evaling.cli.scaffold import MODEL_BLOCKS
from evaling.providers.pricing import PRICES, PRICING_AS_OF, Price, estimate_cost, price_for


def test_known_model_cost():
    # 1M in @ $2 + 1M out @ $10
    assert estimate_cost("claude-sonnet-5", 1_000_000, 1_000_000) == pytest.approx(12.0)


def test_a_rate_that_was_cut_is_the_new_one():
    """gpt-5.6-sol went from $5/$30 to $4/$20; nothing pinned it, so the stale
    figure sat in the table for two weeks reading double on output."""
    assert estimate_cost("gpt-5.6-sol", 1_000_000, 1_000_000) == pytest.approx(24.0)


def test_small_usage_precision():
    assert estimate_cost("claude-opus-5", 1000, 500) == pytest.approx(1000 * 5e-6 + 500 * 25e-6)


def test_cyber_model_uses_its_published_rate():
    assert estimate_cost("gpt-5.6-cyber", 1_000_000, 0) == pytest.approx(12.50)
    assert estimate_cost("gpt-5.6-cyber", 0, 1_000_000) == pytest.approx(75.00)


def test_models_added_on_the_2026_10_23_review():
    """One pin per card, so an edit that drops or retypes an entry is noticed."""
    assert estimate_cost("claude-opus-5-5", 1_000_000, 1_000_000) == pytest.approx(24.0)
    assert estimate_cost("gpt-6-luna", 1_000_000, 1_000_000) == pytest.approx(0.60)
    assert estimate_cost("gemini-4-argon", 1_000_000, 1_000_000) == pytest.approx(24.0)


def test_argon_carries_its_post_introductory_rate():
    """$2/$10 through 2026-11-30, $4/$20 after. The table takes the later rate
    so the estimate reads high now rather than low later (RELEASING.md step 1)."""
    assert PRICES["gemini-4-argon"] == Price(4.00, 20.00)


def test_haiku_5_5_takes_the_short_prompt_tier():
    """Prompts over 100k input tokens bill five times more; ordinary evals
    fall in the lower tier, which is the one the table carries."""
    assert estimate_cost("claude-haiku-5-5", 1_000_000, 1_000_000) == pytest.approx(0.60)


def test_delisted_but_still_served_models_keep_their_last_rate():
    """These left the published cards but the APIs still list them; an unknown
    cost would be the worse answer."""
    for model in ("gpt-5.3-chat-latest", "gpt-3.5-turbo-1106", "gemini-3.7-flash"):
        assert model in PRICES, model


def test_unknown_model_has_no_cost():
    assert estimate_cost("some-local-llama", 100, 100) is None


def test_config_pricing_override():
    params = {"pricing": {"input": 2.0, "output": 4.0}}
    assert estimate_cost("some-local-llama", 1_000_000, 1_000_000, params) == pytest.approx(6.0)


def test_override_beats_the_table():
    params = {"pricing": {"input": 0.0, "output": 0.0}}
    assert estimate_cost("claude-opus-5", 1_000_000, 1_000_000, params) == 0.0


def test_partial_override_ignored():
    # a half-specified override would silently mis-price; fall back to the table
    assert price_for("claude-opus-5", {"pricing": {"input": 1.0}}) == PRICES["claude-opus-5"]


def test_absent_usage_is_unknown_not_free():
    # A priced model whose endpoint reports no usage costs an unknown amount.
    # Calling it $0 would silently under-count spend against --max-cost.
    assert estimate_cost("claude-opus-5", None, None) is None
    assert estimate_cost("claude-opus-5", 1_000_000, None) == pytest.approx(5.0)


@pytest.mark.parametrize(
    "pricing",
    [
        {"input": -5, "output": 10},  # negative would shrink tracked spend
        {"input": "free", "output": 1},  # non-numeric used to raise mid-run
        {"input": None, "output": None},
    ],
)
def test_malformed_override_never_crashes_or_goes_negative(pricing):
    # The schema rejects these at load time; price_for stays defensive so a
    # pricing problem can never destroy an already-paid-for response.
    price = price_for("claude-opus-5", {"pricing": pricing})
    assert price == PRICES["claude-opus-5"]
    cost = estimate_cost("claude-opus-5", 1000, 1000, {"pricing": pricing})
    assert cost is not None and cost > 0


def test_table_entries_are_positive():
    for model, price in PRICES.items():
        assert price.input > 0 and price.output > 0, model
        assert price.output >= price.input, model  # output always costs at least input


#: Scaffolds whose model is deliberately unpriced. `mock` costs nothing;
#: `openai-compatible` points at an endpoint whose operator sets the rates,
#: which no table can know.
UNPRICED_SCAFFOLDS = {"mock", "openai-compatible"}


def test_every_model_evaling_itself_suggests_is_priced():
    """`init --provider X` writes a model id into the user's config. An
    unpriced one means their very first real run reports no cost at all — the
    estimate line is omitted entirely rather than showing $0.00.

    Scoped by provider rather than by an id pattern: the first version of this
    matched `claude-*` only, so it passed while `init --provider openai`
    scaffolded a `gpt-5.2` that had no price.
    """
    checked = 0
    for provider, block in MODEL_BLOCKS.items():
        if provider in UNPRICED_SCAFFOLDS:
            continue
        for model in re.findall(r"id:\s*([\w.:-]+)", block):
            assert model in PRICES, f"`init --provider {provider}` scaffolds unpriced {model}"
            checked += 1
    assert checked, "no scaffolded models were checked"


def test_the_table_is_dated():
    """A rate card with no date cannot be checked against a published one."""
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", PRICING_AS_OF)
