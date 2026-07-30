"""Smaller defects from a whole-codebase audit, each one a quiet wrong answer.

None of these crash. They report a number that is not true, ignore a flag
that was given, or turn a model's nonsense into a perfect score — which is
the failure mode this project treats as worse than an exception.
"""

import json
import re

import pytest
from click.testing import CliRunner

from evaling.cli import main
from evaling.config import Settings, load_config
from evaling.engine import run_eval
from evaling.export import export_run
from evaling.scorers.base import ScoringError
from evaling.storage import ResultRecord, RunStore

ENV = {"EVALING_USER_CONFIG": "/nonexistent"}

CONFIG = (
    "models: [{id: mock, provider: mock, params: {cost: 0.5}}]\n"
    "variants:\n  - name: v1\n"
    '    prompt: [{role: user, content: "{{ q }}"}]\n'
    "cases: [{id: c1, vars: {q: a}, expected: a}, {id: c2, vars: {q: b}, expected: NOPE}]\n"
    "scorecard: [{criterion: acc, scorer: {type: exact}}]\n"
)


@pytest.fixture
def project(tmp_path):
    (tmp_path / "eval.yaml").write_text(CONFIG, encoding="utf-8")
    return tmp_path


def invoke(path, *args):
    return CliRunner().invoke(
        main,
        ["-c", str(path / "eval.yaml"), "-o", str(path / "runs"), *args],
        env=ENV,
        catch_exceptions=False,
    )


class TestShowFailuresJson:
    """`--failures` was accepted and then ignored on the JSON path."""

    def test_it_narrows_to_failures(self, project):
        invoke(project, "run")
        payload = json.loads(invoke(project, "--json", "show", "latest", "--failures").output)
        assert [r["case_id"] for r in payload["results"]] == ["c2"]

    def test_without_it_everything_is_returned(self, project):
        invoke(project, "run")
        payload = json.loads(invoke(project, "--json", "show", "latest").output)
        assert len(payload["results"]) == 2

    def test_it_composes_with_case(self, project):
        invoke(project, "run")
        payload = json.loads(
            invoke(project, "--json", "show", "latest", "--failures", "--case", "c1").output
        )
        assert payload["results"] == [], "c1 passed, so it is not a failure"


class TestACachedRunReportsNoSpend:
    """`totals.cost_usd` is documented as what the run actually cost."""

    def settings(self, path):
        return Settings.model_validate(
            {"output_dir": str(path / "runs"), "cache_dir": str(path / "cache"), "cache": True}
        )

    def test_a_fully_cached_rerun_costs_nothing(self, project):
        config = load_config(project / "eval.yaml")
        first = run_eval(config, self.settings(project))
        assert first.totals["cost_usd"] == 1.0

        second = run_eval(config, self.settings(project))
        assert second.counts["cached"] == 2, "the cache was not used"
        assert second.totals["cost_usd"] == 0.0

    def test_the_record_still_says_what_it_would_have_cost(self, project):
        """Useful to know; just not a claim about money that was spent."""
        config = load_config(project / "eval.yaml")
        run_eval(config, self.settings(project))
        second = run_eval(config, self.settings(project))
        assert all(record.cost_usd == 0.5 for record in second.records)


class TestNonNumericScorerParams:
    """ScorerSpec allows extra keys, so the schema accepts anything here."""

    def test_a_bad_tolerance_is_a_message(self, project):
        from evaling.scorers.agreement import AgreementScorer

        with pytest.raises(ScoringError, match="must be a number"):
            AgreementScorer({"tolerance": "loose"}, None)

    def test_a_bad_scale_is_a_message_not_a_traceback(self, project):
        (project / "eval.yaml").write_text(
            CONFIG.replace(
                "scorecard: [{criterion: acc, scorer: {type: exact}}]\n",
                "scorecard: [{criterion: g, scorer: {type: llm-judge, judge: j, scale: wide}}]\n"
                "judges:\n  j: {model: judge-m, rubric: [{role: user, content: grade}]}\n",
            ).replace(
                "models: [{id: mock, provider: mock, params: {cost: 0.5}}]",
                "models: [{id: mock, provider: mock}, {id: judge-m, provider: mock, role: judge}]",
            ),
            encoding="utf-8",
        )
        result = invoke(project, "run")
        assert result.exit_code == 2
        assert "must be a number" in result.output
        assert "Traceback" not in result.output


class TestABomDoesNotBreakADataset:
    """Excel writes one, and it is invisible to whoever saved the file."""

    def test_the_first_column_is_still_usable(self, tmp_path):
        (tmp_path / "cases.csv").write_bytes("﻿id,question\nc1,hello\n".encode())
        (tmp_path / "eval.yaml").write_text(
            "models: [{id: mock, provider: mock}]\n"
            "variants:\n  - name: v1\n"
            '    prompt: [{role: user, content: "{{ question }}"}]\n'
            "cases: {file: cases.csv}\n"
            'scorecard: [{criterion: acc, scorer: {type: contains, value: ""}}]\n',
            encoding="utf-8",
        )
        result = invoke(tmp_path, "run")
        assert result.exit_code == 0, result.output
        assert "1/1 succeeded" in result.output


class TestMarkdownCellsSurviveHostileNames:
    def test_a_pipe_in_a_name_does_not_break_the_table(self):
        meta = {
            "id": "r1",
            "label": "a | label",
            "status": "complete",
            "counts": {"total": 1},
            "totals": {"cost_usd": 0.0},
            "aggregates": {
                "overall": {"cases": 1, "score": 1.0, "pass_rate": 1.0, "errors": 0},
                "matrix": [
                    {
                        "variant": "v|1",
                        "model": "m|1",
                        "cases": 1,
                        "score": 1.0,
                        "pass_rate": 1.0,
                        "errors": 0,
                    }
                ],
            },
        }
        record = ResultRecord(variant="v|1", model="m|1", case_id="c|1")
        record.scores = {"acc": {"weight": 1.0, "score": 1.0, "passed": True}}
        text = export_run(meta, [record], "md")
        rows = [line for line in text.splitlines() if line.startswith("|")]
        # Unescaped pipes only: an escaped one is still a `|` character but is
        # no longer a cell separator, which is the whole point.
        widths = {len(re.findall(r"(?<!\\)\|", line)) for line in rows}
        assert len(widths) == 1, f"a name broke the table into widths {widths}"
        assert "v\\|1" in text, "the name was not escaped at all"


class TestAMalformedRunJsonDoesNotPoisonTheListing:
    def test_listing_survives(self, project, tmp_path):
        invoke(project, "run")
        broken = project / "runs" / "not-a-run"
        broken.mkdir(parents=True)
        (broken / "run.json").write_text("null", encoding="utf-8")

        result = invoke(project, "list")
        assert result.exit_code == 0, result.output
        assert RunStore(project / "runs").list_runs(), "the good run vanished too"
