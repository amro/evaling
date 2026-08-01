import re

import pytest

from evaling.cli.scaffold import MODEL_BLOCKS
from evaling.providers.pricing import PRICES, PRICING_AS_OF, estimate_cost, price_for


def test_known_model_cost():
    # 1M in @ $3 + 1M out @ $15
    assert estimate_cost("claude-sonnet-5", 1_000_000, 1_000_000) == pytest.approx(18.0)


def test_small_usage_precision():
    assert estimate_cost("claude-opus-5", 1000, 500) == pytest.approx(1000 * 5e-6 + 500 * 25e-6)


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


def test_every_model_evaling_itself_suggests_is_priced():
    """`init --provider anthropic` scaffolds a model id; an unpriced one makes
    the very first real run report an unknown cost."""
    suggested = re.findall(r"id:\s*(claude-[\w.-]+)", "\n".join(MODEL_BLOCKS.values()))
    assert suggested
    for model in suggested:
        assert model in PRICES, f"{model} is scaffolded but has no price"


def test_the_table_is_dated():
    """A rate card with no date cannot be checked against a published one."""
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", PRICING_AS_OF)
