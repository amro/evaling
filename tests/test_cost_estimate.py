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
from evaling.providers.pricing import estimate_run

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
        path = project(tmp_path, PRICED_CAPPED + UNPRICED)
        assert "no pricing" in self.invoke(path, "run").output

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
