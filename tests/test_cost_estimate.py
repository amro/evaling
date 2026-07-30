"""What a run is likely to cost, said before it runs.

This replaced a confirmation prompt above a fixed cell count. The count was a
poor proxy — a hundred cells against a local model costs nothing — so the
prompt fired on ordinary work, and a guard that fires on ordinary work is
bypassed reflexively rather than read. The number is the useful part; the
question was not.
"""

import pytest
from click.testing import CliRunner

from evaling.cli import main
from evaling.config import load_config
from evaling.engine import estimate_run_cost, select_matrix
from evaling.providers.pricing import ASSUMED_OUTPUT_TOKENS, estimate_run, price_for

ENV = {"EVALING_USER_CONFIG": "/nonexistent"}


def project(tmp_path, models, cases=2):
    tmp_path.mkdir(parents=True, exist_ok=True)
    ids = ", ".join(f"{{id: c{i}, vars: {{q: '{i}'}}}}" for i in range(cases))
    (tmp_path / "eval.yaml").write_text(
        f"models:\n{models}"
        "variants:\n  - name: v1\n"
        '    prompt: [{role: user, content: "answer {{ q }}"}]\n'
        f"cases: [{ids}]\n"
        'scorecard: [{criterion: acc, scorer: {type: contains, value: ""}}]\n',
        encoding="utf-8",
    )
    return tmp_path


def estimate_for(tmp_path):
    config = load_config(tmp_path / "eval.yaml")
    variants, models, cases = select_matrix(config)
    return estimate_run_cost(config, variants, models, cases)


PRICED_CAPPED = "  - {id: claude-sonnet-5, provider: anthropic, params: {max_tokens: 256}}\n"
PRICED_UNCAPPED = "  - {id: claude-opus-5, provider: anthropic}\n"
UNPRICED = "  - {id: local, provider: openai-compatible, base_url: 'http://x/v1'}\n"


class TestTheEstimate:
    def test_a_priced_model_is_estimated(self, tmp_path):
        estimate = estimate_for(project(tmp_path, PRICED_CAPPED))
        assert estimate.priced is True
        assert estimate.usd > 0

    def test_an_uncapped_model_is_estimated_too(self, tmp_path):
        """No max_tokens means output length is assumed, not that we give up."""
        estimate = estimate_for(project(tmp_path, PRICED_UNCAPPED))
        assert estimate.usd > 0

    def test_it_scales_with_the_matrix(self, tmp_path):
        small = estimate_for(project(tmp_path / "a", PRICED_CAPPED, cases=2))
        large = estimate_for(project(tmp_path / "b", PRICED_CAPPED, cases=20))
        assert large.usd > small.usd * 5

    def test_an_unpriced_model_is_named_not_counted_as_free(self, tmp_path):
        estimate = estimate_for(project(tmp_path, PRICED_CAPPED + UNPRICED))
        assert estimate.priced is False
        assert estimate.unpriced == ("local",)
        assert estimate.usd > 0, "the priced model should still be counted"

    def test_nothing_priced_gives_no_estimate(self, tmp_path):
        """None rather than $0.00, which would read as free."""
        assert estimate_for(project(tmp_path, UNPRICED)) is None

    def test_no_cases_gives_no_estimate(self):
        assert estimate_run([]) is None


class TestWhatTheRunSays:
    def invoke(self, path, *args):
        return CliRunner().invoke(
            main,
            ["-c", str(path / "eval.yaml"), "-o", str(path / "runs"), *args],
            env=ENV,
            catch_exceptions=False,
        )

    def test_a_dry_run_shows_the_estimate(self, tmp_path):
        """A dry run asks "what would this do"; cost is half the answer."""
        path = project(tmp_path, PRICED_CAPPED)
        result = self.invoke(path, "run", "--dry-run")
        assert result.exit_code == 0, result.output
        assert "estimated" in result.output

    def test_validate_shows_it_too(self, tmp_path):
        path = project(tmp_path, PRICED_CAPPED)
        assert "estimated" in self.invoke(path, "validate").output

    def test_the_figure_is_presented_as_an_estimate(self, tmp_path):
        """Token counts are approximate, prices drift, judges are uncounted."""
        path = project(tmp_path, PRICED_CAPPED, cases=200)
        output = self.invoke(path, "run").output
        assert "estimated ~$" in output
        for overclaim in ("at most", "exactly", "will cost"):
            assert overclaim not in output

    def test_a_tiny_estimate_does_not_round_to_zero(self, tmp_path):
        """ "$0.00" reads as free; "under $0.01" reads as cheap."""
        path = project(tmp_path, PRICED_CAPPED, cases=1)
        assert "under $0.01" in self.invoke(path, "run").output

    def test_an_unpriced_model_is_named(self, tmp_path):
        # --dry-run because the claim is about the estimate, and the unpriced
        # model here is a local endpoint: a real `run` would try to reach it.
        path = project(tmp_path, PRICED_CAPPED + UNPRICED)
        assert "no pricing" in self.invoke(path, "run", "--dry-run").output

    def test_an_unpriced_run_says_nothing_about_money(self, tmp_path):
        """Better silent than a figure nobody should trust."""
        path = project(tmp_path, "  - {id: mock, provider: mock}\n")
        output = self.invoke(path, "run").output
        assert "$" not in output.split("succeeded")[0]

    @pytest.mark.parametrize("cases", [2, 200])
    def test_no_size_is_a_decision_point(self, tmp_path, cases):
        """Large or small, the run proceeds — there is nothing to answer."""
        path = project(tmp_path, "  - {id: mock, provider: mock}\n", cases=cases)
        result = self.invoke(path, "run")
        assert result.exit_code == 0, result.output
        assert "continue?" not in result.output.lower()


class TestJudgesAreCounted:
    """A judged run makes more than one call per cell.

    Leaving judges out understated a judged run by roughly half, in the
    direction that matters: two judged criteria means three calls per cell,
    not one.
    """

    def config(self, tmp_path, criteria):
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "rubric.yaml").write_text(
            "- {role: user, content: 'Grade this answer against the rubric: {{ output }}'}\n",
            encoding="utf-8",
        )
        (tmp_path / "eval.yaml").write_text(
            "models:\n"
            "  - {id: claude-sonnet-5, provider: anthropic, params: {max_tokens: 256}}\n"
            "  - id: claude-opus-5\n"
            "    provider: anthropic\n"
            "    role: judge\n"
            "    params: {max_tokens: 256}\n"
            "variants:\n  - name: v1\n"
            '    prompt: [{role: user, content: "answer {{ q }}"}]\n'
            "cases: [{id: c1, vars: {q: a}}, {id: c2, vars: {q: b}}]\n"
            f"scorecard:\n{criteria}"
            "judges:\n  j: {model: claude-opus-5, rubric: rubric.yaml}\n",
            encoding="utf-8",
        )
        return estimate_for(tmp_path)

    PLAIN = '  - {criterion: acc, scorer: {type: contains, value: ""}}\n'
    JUDGED = "  - {criterion: g1, scorer: {type: llm-judge, judge: j}}\n"

    def test_a_judged_criterion_costs_more_than_none(self, tmp_path):
        plain = self.config(tmp_path / "plain", self.PLAIN)
        judged = self.config(tmp_path / "judged", self.PLAIN + self.JUDGED)
        assert judged.usd > plain.usd, "the judge's calls were not counted"

    def test_two_judged_criteria_cost_more_than_one(self, tmp_path):
        one = self.config(tmp_path / "one", self.JUDGED)
        two = self.config(
            tmp_path / "two",
            self.JUDGED + "  - {criterion: g2, scorer: {type: llm-judge, judge: j}}\n",
        )
        assert two.usd > one.usd * 1.5, "a second judged criterion added nothing"

    def test_the_judge_model_is_the_one_priced(self, tmp_path):
        """The judge's own model and params, not the candidate's."""
        judged = self.config(tmp_path / "j", self.JUDGED)
        assert judged.priced is True
        assert judged.unpriced == ()

    def test_an_unpriced_judge_is_named(self, tmp_path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "rubric.yaml").write_text(
            "- {role: user, content: 'grade {{ output }}'}\n", encoding="utf-8"
        )
        (tmp_path / "eval.yaml").write_text(
            "models:\n"
            "  - {id: claude-sonnet-5, provider: anthropic, params: {max_tokens: 256}}\n"
            "  - id: local-judge\n"
            "    provider: openai-compatible\n"
            "    base_url: 'http://x/v1'\n"
            "    role: judge\n"
            "variants:\n  - name: v1\n"
            '    prompt: [{role: user, content: "answer {{ q }}"}]\n'
            "cases: [{id: c1, vars: {q: a}}]\n"
            "scorecard:\n  - {criterion: g, scorer: {type: llm-judge, judge: j}}\n"
            "judges:\n  j: {model: local-judge, rubric: rubric.yaml}\n",
            encoding="utf-8",
        )
        estimate = estimate_for(tmp_path)
        assert estimate.unpriced == ("local-judge",)


MODEL = "claude-sonnet-5"


class TestTheEstimateArithmetic:
    """The sums behind the figure, exercised directly on groups.

    Every other test here goes through a config, which fixes the token counts
    and the cell count together — so mutation testing found the arithmetic
    barely pinned at all. Dropping the input half entirely, or dividing by the
    cell count instead of multiplying, both survived the suite.
    """

    def price(self):
        price = price_for(MODEL, {})
        assert price is not None, f"{MODEL} is expected to be in the built-in table"
        return price

    def test_both_halves_scale_with_the_cell_count(self):
        price, cells, input_each = self.price(), 10, 1000
        estimate = estimate_run([(MODEL, {}, input_each, cells)])
        expected = (
            input_each * cells * price.input + ASSUMED_OUTPUT_TOKENS * cells * price.output
        ) / 1_000_000
        assert estimate.usd == pytest.approx(round(expected, 4))

    def test_the_input_half_is_actually_counted(self):
        """It contributed nothing and no test noticed."""
        without = estimate_run([(MODEL, {}, 0, 10)]).usd
        with_input = estimate_run([(MODEL, {}, 5000, 10)]).usd
        assert with_input > without

    def test_ten_times_the_cells_costs_ten_times_as_much(self):
        one = estimate_run([(MODEL, {}, 1000, 1)]).usd
        ten = estimate_run([(MODEL, {}, 1000, 10)]).usd
        # Each figure is rounded to 4 decimals, so at these magnitudes the
        # rounding alone can move the ratio by ~0.5%. The tolerance covers
        # that; the mutations this is here to catch — dropping the input half,
        # or dividing by the cell count instead of multiplying — are off by
        # factors, not percentages.
        assert ten == pytest.approx(one * 10, rel=1e-2)


class TestMaxTokensCapsTheOutputHalf:
    """`max_tokens` is the only thing that makes the output guess better."""

    def test_a_cap_lowers_the_estimate(self):
        capped = estimate_run([(MODEL, {"max_tokens": 100}, 100, 10)]).usd
        uncapped = estimate_run([(MODEL, {}, 100, 10)]).usd
        assert capped < uncapped

    def test_the_cap_is_the_number_used(self):
        price = price_for(MODEL, {})
        estimate = estimate_run([(MODEL, {"max_tokens": 100}, 0, 10)])
        assert estimate.usd == pytest.approx(round(100 * 10 * price.output / 1_000_000, 4))

    @pytest.mark.parametrize("cap", [0, -1, "many", None, 2.5])
    def test_a_cap_that_is_not_a_positive_count_falls_back(self, cap):
        """Zero is the interesting one: it is a number, and it is not a cap."""
        fallback = estimate_run([(MODEL, {}, 100, 10)]).usd
        assert estimate_run([(MODEL, {"max_tokens": cap}, 100, 10)]).usd == fallback

    def test_a_cap_of_one_is_honoured(self):
        """The boundary: 1 is a positive count, however silly."""
        tiny = estimate_run([(MODEL, {"max_tokens": 1}, 0, 10)]).usd
        assert tiny < estimate_run([(MODEL, {}, 0, 10)]).usd


class TestWhichNameIsPriced:
    """An `openai-compatible` model's `params.model` names the real model.

    The evaling-side id is arbitrary — `local`, `prod-gateway` — so pricing has
    to look at what is actually sent.
    """

    def test_params_model_wins_over_the_evaling_id(self):
        estimate = estimate_run([("my-alias", {"model": MODEL}, 100, 1)])
        assert estimate is not None and estimate.priced and estimate.usd > 0

    def test_without_it_the_id_is_what_gets_looked_up(self):
        assert estimate_run([("my-alias", {}, 100, 1)]) is None


class TestAnUnpricedGroupDoesNotStopTheRest:
    def test_a_priced_group_after_an_unpriced_one_still_counts(self):
        estimate = estimate_run([("unknown-model", {}, 100, 1), (MODEL, {}, 100, 1)])
        assert estimate is not None
        assert estimate.usd > 0
        assert estimate.unpriced == ("unknown-model",)
        assert estimate.priced is False, "part of the run has no price"

    def test_every_unpriced_model_is_named_once(self):
        estimate = estimate_run(
            [("a", {}, 1, 1), ("b", {}, 1, 1), ("a", {}, 1, 1), (MODEL, {}, 1, 1)]
        )
        assert estimate.unpriced == ("a", "b")
