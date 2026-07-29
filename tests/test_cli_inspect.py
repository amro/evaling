import json

import pytest
from click.testing import CliRunner

from evaling.cli import main

ENV = {"EVALING_USER_CONFIG": "/nonexistent"}

GOOD = """\
models: [{id: mock, provider: mock}]
variants:
  - name: v1
    prompt: [{role: user, content: "{{ q }}"}]
cases:
  - {id: c1, vars: {q: alpha}, expected: alpha}
  - {id: c2, vars: {q: beta}, expected: beta}
scorecard: [{criterion: acc, scorer: {type: exact}}]
"""

MIXED = GOOD.replace("expected: beta", "expected: WRONG")


def cli(tmp_path, *args):
    base = ["-o", str(tmp_path / "runs"), "--cache-dir", str(tmp_path / "cache")]
    return CliRunner().invoke(main, base + list(args), env=ENV, catch_exceptions=False)


@pytest.fixture
def two_runs(tmp_path):
    (tmp_path / "good.yaml").write_text(GOOD, encoding="utf-8")
    (tmp_path / "mixed.yaml").write_text(MIXED, encoding="utf-8")
    assert cli(tmp_path, "-q", "run", str(tmp_path / "good.yaml"), "--label", "good").exit_code == 0
    assert cli(tmp_path, "-q", "run", str(tmp_path / "mixed.yaml")).exit_code == 0
    return tmp_path


def test_list_newest_first_with_limit(two_runs):
    runs = json.loads(cli(two_runs, "--json", "list").output)
    assert len(runs) == 2
    assert runs[0]["id"] >= runs[1]["id"]
    assert runs[1]["label"] == "good"

    limited = json.loads(cli(two_runs, "--json", "list", "--limit", "1").output)
    assert len(limited) == 1


def test_show_summary_by_label_and_latest(two_runs):
    result = cli(two_runs, "show", "good")
    assert result.exit_code == 0
    assert "(good)" in result.output
    assert "100.0%" in result.output

    latest = cli(two_runs, "show", "latest")
    assert "50.0%" in latest.output


def test_show_failures(two_runs):
    result = cli(two_runs, "show", "latest", "--failures")
    assert result.exit_code == 0
    assert "c2" in result.output
    assert "expected 'WRONG'" in result.output

    clean = cli(two_runs, "show", "good", "--failures")
    assert "no failures" in clean.output


def test_show_case_drilldown(two_runs):
    result = cli(two_runs, "show", "latest", "--case", "c1")
    assert result.exit_code == 0
    assert "alpha" in result.output

    missing = cli(two_runs, "show", "latest", "--case", "ghost")
    assert missing.exit_code == 2
    assert "no results for case" in missing.output


def test_show_unknown_ref_exits_2(two_runs):
    result = cli(two_runs, "show", "nonesuch")
    assert result.exit_code == 2
    assert "no run matches" in result.output


def test_compare(two_runs):
    result = cli(two_runs, "compare", "good", "latest")
    assert result.exit_code == 0
    assert "overall: score 1.000 → 0.500" in result.output
    assert "-50.0%" in result.output


def test_export_stdout_and_file(two_runs):
    md = cli(two_runs, "export", "good", "--format", "md")
    assert md.exit_code == 0
    assert md.output.startswith("# evaling run")

    out_file = two_runs / "report.csv"
    csv_result = cli(two_runs, "export", "good", "--format", "csv", "--out", str(out_file))
    assert csv_result.exit_code == 0
    assert out_file.read_text(encoding="utf-8").startswith("variant,model,case_id")

    data = json.loads(cli(two_runs, "export", "good", "--format", "json").output)
    assert len(data["results"]) == 2


def test_baseline_pin_and_regression_gate(two_runs):
    assert cli(two_runs, "baseline", "set", "good").exit_code == 0
    shown = cli(two_runs, "baseline", "show")
    assert shown.output.strip() != "no baseline pinned"

    # a run gated with baseline: regression against the pinned (perfect) run
    gated = GOOD + "thresholds: {baseline: regression}\n"
    (two_runs / "gated.yaml").write_text(gated, encoding="utf-8")
    same = cli(two_runs, "run", str(two_runs / "gated.yaml"))
    assert same.exit_code == 0, same.output

    worse = gated.replace("expected: beta", "expected: WRONG")
    (two_runs / "worse.yaml").write_text(worse, encoding="utf-8")
    regressed = cli(two_runs, "run", str(two_runs / "worse.yaml"))
    assert regressed.exit_code == 1
    assert "baseline" in regressed.output


def test_regression_gate_without_pinned_baseline_exits_2(two_runs):
    gated = GOOD + "thresholds: {baseline: regression}\n"
    (two_runs / "gated.yaml").write_text(gated, encoding="utf-8")
    result = cli(two_runs, "run", str(two_runs / "gated.yaml"))
    assert result.exit_code == 2
    assert "no baseline pinned" in result.output


def test_baseline_show_unpinned(two_runs):
    result = cli(two_runs, "baseline", "show")
    assert "no baseline pinned" in result.output


def test_export_out_to_missing_directory_exits_2_cleanly(two_runs):
    result = cli(
        two_runs,
        "export",
        "good",
        "--format",
        "md",
        "--out",
        str(two_runs / "nonexistent-dir" / "report.md"),
    )
    assert result.exit_code == 2
    assert "Traceback" not in result.output


class TestJudgeOnlyModelsAreVisible:
    """The original defect was invisibility, not the default."""

    def config(self, tmp_path, role="judge"):
        (tmp_path / "rubric.yaml").write_text("- role: user\n  content: '{{ output }}'\n")
        path = tmp_path / "eval.yaml"
        path.write_text(
            "models:\n"
            "  - {id: main, provider: mock}\n"
            f"  - {{id: grader, provider: mock, role: {role}, "
            "params: {response: '{\"score\": 1.0}'}}\n"
            "variants:\n  - name: v\n"
            '    prompt: [{role: user, content: "{{ q }}"}]\n'
            "cases: [{id: c1, vars: {q: hi}}]\n"
            "judges: {q: {model: grader, rubric: rubric.yaml}}\n"
            "scorecard: [{criterion: g, scorer: {type: llm-judge, judge: q}}]\n"
        )
        return path

    def run(self, tmp_path, *args):
        base = ["-o", str(tmp_path / "runs"), "--cache-dir", str(tmp_path / "c")]
        return CliRunner().invoke(main, base + list(args), env=ENV, catch_exceptions=False)

    def test_the_header_names_a_judge_only_model(self, tmp_path):
        result = self.run(tmp_path, "run", str(self.config(tmp_path)))
        assert result.exit_code == 0, result.output
        assert "grader: judge only, not evaluated" in result.output

    def test_role_both_is_not_announced_as_judge_only(self, tmp_path):
        result = self.run(tmp_path, "run", str(self.config(tmp_path, role="both")))
        assert "judge only" not in result.output

    def test_filtering_to_a_judge_only_model_explains_itself(self, tmp_path):
        result = self.run(tmp_path, "run", "--model", "grader", str(self.config(tmp_path)))
        assert result.exit_code == 2
        # rich wraps the message, so compare with whitespace collapsed.
        message = " ".join(result.output.split())
        assert "role 'judge'" in message and "so it is not evaluated" in message
