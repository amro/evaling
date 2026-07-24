import json

from click.testing import CliRunner

from evaling.cli import main

ENV = {"EVALING_USER_CONFIG": "/nonexistent"}

CONFIG = """\
models: [{id: mock, provider: mock}]
variants:
  - name: v1
    prompt: [{role: user, content: "{{ q }}"}]
cases:
  - {id: c1, vars: {q: alpha}, expected: alpha}
  - {id: c2, vars: {q: beta}, expected: beta}
scorecard: [{criterion: acc, scorer: {type: exact}}]
"""


def invoke(tmp_path, *args, config=CONFIG, config_name="eval.yaml"):
    if config is not None:
        (tmp_path / config_name).write_text(config)
    base = [
        "-o",
        str(tmp_path / "runs"),
        "--cache-dir",
        str(tmp_path / "cache"),
    ]
    return CliRunner().invoke(main, base + list(args), env=ENV, catch_exceptions=False)


def run_args(tmp_path, *extra):
    return ("run", str(tmp_path / "eval.yaml"), *extra)


def test_run_happy_path(tmp_path):
    result = invoke(tmp_path, *run_args(tmp_path))
    assert result.exit_code == 0, result.output
    assert "2 requests" in result.output
    assert "v1" in result.output
    assert "2/2 succeeded" in result.output
    assert "stored in" in result.output


def test_run_json_output(tmp_path):
    result = invoke(tmp_path, "--json", *run_args(tmp_path))
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["counts"] == {"total": 2, "succeeded": 2, "failed": 0, "cached": 0}
    assert payload["aggregates"]["overall"]["pass_rate"] == 1.0
    assert payload["gate"] is None


def test_run_gate_failure_exits_1(tmp_path):
    config = CONFIG.replace("expected: beta", "expected: WRONG")
    config += "thresholds: {min_pass_rate: 0.9}\n"
    result = invoke(tmp_path, *run_args(tmp_path), config=config)
    assert result.exit_code == 1
    assert "gate FAILED" in result.output
    assert "min_pass_rate" in result.output


def test_dry_run_clean(tmp_path):
    result = invoke(tmp_path, *run_args(tmp_path, "--dry-run"))
    assert result.exit_code == 0
    assert "2 requests would be made" in result.output
    assert (tmp_path / "runs").exists() is False


def test_dry_run_reports_template_errors_and_exits_2(tmp_path):
    config = CONFIG.replace("{{ q }}", "{{ missing }}")
    result = invoke(tmp_path, *run_args(tmp_path, "--dry-run"), config=config)
    assert result.exit_code == 2
    assert "'missing' is undefined" in result.output


def test_case_filter(tmp_path):
    result = invoke(tmp_path, "--json", *run_args(tmp_path, "--case", "c1"))
    assert json.loads(result.output)["counts"]["total"] == 1


def test_unknown_model_filter_exits_2(tmp_path):
    result = invoke(tmp_path, *run_args(tmp_path, "--model", "ghost"))
    assert result.exit_code == 2
    assert "unknown model" in result.output


def test_missing_config_exits_2(tmp_path):
    result = invoke(tmp_path, "run", str(tmp_path / "nope.yaml"), config=None)
    assert result.exit_code == 2
    assert "not found" in result.output


def test_cache_on_by_default_and_no_cache_bypasses(tmp_path):
    invoke(tmp_path, *run_args(tmp_path))
    second = invoke(tmp_path, "--json", *run_args(tmp_path))
    assert json.loads(second.output)["counts"]["cached"] == 2

    bypassed = invoke(tmp_path, "--json", *run_args(tmp_path, "--no-cache"))
    assert json.loads(bypassed.output)["counts"]["cached"] == 0


def test_quiet_run_prints_nothing_on_success(tmp_path):
    result = invoke(tmp_path, "-q", *run_args(tmp_path))
    assert result.exit_code == 0
    assert result.output == ""


def test_config_flag_alternative_location(tmp_path):
    (tmp_path / "custom.yaml").write_text(CONFIG)
    result = invoke(tmp_path, "-c", str(tmp_path / "custom.yaml"), "run", config=None)
    assert result.exit_code == 0, result.output
