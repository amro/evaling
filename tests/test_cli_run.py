import json
import os

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
        (tmp_path / config_name).write_text(config, encoding="utf-8")
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
    (tmp_path / "custom.yaml").write_text(CONFIG, encoding="utf-8")
    result = invoke(tmp_path, "-c", str(tmp_path / "custom.yaml"), "run", config=None)
    assert result.exit_code == 0, result.output


def test_unknown_case_filter_fails_before_progress(tmp_path):
    # Regression: the CLI used to print "Running 0 requests" and a 0/0 bar
    # before the engine rejected the bogus id.
    result = invoke(tmp_path, *run_args(tmp_path, "--case", "bogus"))
    assert result.exit_code == 2
    assert "unknown case id" in result.output
    assert "Running" not in result.output


def test_reserved_label_rejected(tmp_path):
    result = invoke(tmp_path, *run_args(tmp_path, "--label", "latest"))
    assert result.exit_code == 2
    assert "reserved" in result.output


def test_config_settings_output_dir_respected_by_all_commands(tmp_path, monkeypatch):
    # Regression: run honored settings.output_dir from eval.yaml but
    # show/list/baseline resolved the default dir and found nothing.
    monkeypatch.chdir(tmp_path)
    config = CONFIG + "settings: {output_dir: custom_runs}\n"
    (tmp_path / "eval.yaml").write_text(config, encoding="utf-8")
    runner_args = ["run", "eval.yaml"]

    from click.testing import CliRunner

    from evaling.cli import main as cli_main

    assert CliRunner().invoke(cli_main, ["-q", *runner_args], env=ENV).exit_code == 0
    assert (tmp_path / "custom_runs").is_dir()

    listed = CliRunner().invoke(cli_main, ["--json", "list"], env=ENV)
    assert len(json.loads(listed.output)) == 1

    shown = CliRunner().invoke(cli_main, ["show", "latest"], env=ENV)
    assert shown.exit_code == 0, shown.output

    pinned = CliRunner().invoke(cli_main, ["baseline", "set", "latest"], env=ENV)
    assert pinned.exit_code == 0, pinned.output
    assert (tmp_path / "custom_runs" / "baseline").is_file()


def test_max_cost_flag_reaches_engine(tmp_path):
    config = CONFIG.replace(
        "models: [{id: mock, provider: mock}]",
        "models: [{id: mock, provider: mock, params: {cost: 1.0}}]",
    )
    result = invoke(
        tmp_path,
        "--json",
        *run_args(tmp_path, "--max-cost", "1.0", "--concurrency", "1"),
        config=config,
    )
    payload = json.loads(result.output)
    # The second cell is skipped, not failed — never attempted, so the run is
    # incomplete and resumable rather than carrying a phantom failure.
    assert payload["counts"]["total"] == 1
    assert payload["counts"]["failed"] == 0
    assert payload["incomplete"] is True
    assert payload["totals"]["cost_usd"] == 1.0
    assert result.exit_code == 1


class TestRunsLiveWithTheirConfig:
    """The cross-directory bug, end to end through the CLI.

    `evaling -c project/eval.yaml run` used to write to ./.evaling/runs, so
    running `evaling list` from inside the project found nothing. No flags
    here: the whole point is what happens when you set none.
    """

    def project(self, tmp_path):
        directory = tmp_path / "project"
        directory.mkdir()
        (directory / "eval.yaml").write_text(CONFIG, encoding="utf-8")
        return directory

    def invoke_from(self, cwd, *args):
        """Run the CLI with the process actually sitting in `cwd`.

        Not CliRunner.isolated_filesystem, which makes a fresh directory
        *inside* the one you pass and chdirs into that instead.
        """
        previous = os.getcwd()
        os.chdir(cwd)
        try:
            return CliRunner().invoke(main, list(args), env=ENV, catch_exceptions=False)
        finally:
            os.chdir(previous)

    def test_a_run_started_elsewhere_is_visible_from_the_project(self, tmp_path):
        project = self.project(tmp_path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()

        started = self.invoke_from(elsewhere, "-c", str(project / "eval.yaml"), "run")
        assert started.exit_code == 0, started.output

        assert (project / ".evaling" / "runs").is_dir(), "the run did not land beside its config"
        listed = self.invoke_from(project, "list")
        assert listed.exit_code == 0, listed.output
        assert "no runs yet" not in listed.output

    def test_the_working_directory_is_left_clean(self, tmp_path):
        project = self.project(tmp_path)
        elsewhere = tmp_path / "elsewhere2"
        elsewhere.mkdir()

        result = self.invoke_from(elsewhere, "-c", str(project / "eval.yaml"), "run")
        assert result.exit_code == 0, result.output
        assert not (elsewhere / ".evaling").exists(), "runs were written to the wrong directory"

    def test_an_explicit_output_dir_still_wins(self, tmp_path):
        """A flag is typed in the moment; it must not be second-guessed."""
        project = self.project(tmp_path)
        target = tmp_path / "explicit"
        result = self.invoke_from(
            tmp_path, "-c", str(project / "eval.yaml"), "-o", str(target), "run"
        )
        assert result.exit_code == 0, result.output
        assert target.is_dir()
        # Only output_dir was overridden, so the cache still anchors to the
        # project -- but no runs may appear there.
        assert not (project / ".evaling" / "runs").exists()


class TestAReservedLabelIsRefusedBeforeTheRunStarts:
    """`latest` and `baseline` mean something to `resolve_ref`.

    A run carrying one would be shadowed by that meaning and unreachable by
    its own name, so the store refuses it. But the store is not created until
    the engine is already underway, so the refusal used to arrive after the
    CLI had printed "Running N requests" and painted a progress bar — nothing
    was spent, but it read as a crash rather than a refusal.
    """

    def test_the_label_is_refused(self, tmp_path):
        result = invoke(tmp_path, *run_args(tmp_path, "--label", "baseline"))
        assert result.exit_code == 2
        assert "reserved as a run reference" in result.output

    def test_latest_too(self, tmp_path):
        result = invoke(tmp_path, *run_args(tmp_path, "--label", "latest"))
        assert result.exit_code == 2

    def test_the_run_never_appears_to_start(self, tmp_path):
        result = invoke(tmp_path, *run_args(tmp_path, "--label", "baseline"))
        assert "requests" not in result.output, "the run announced itself before refusing"

    def test_nothing_is_left_behind(self, tmp_path):
        invoke(tmp_path, *run_args(tmp_path, "--label", "baseline"))
        runs = tmp_path / "runs"
        assert not runs.exists() or not list(runs.glob("*/run.json"))

    def test_an_ordinary_label_still_runs(self, tmp_path):
        result = invoke(tmp_path, *run_args(tmp_path, "--label", "nightly"))
        assert result.exit_code == 0, result.output


class TestCacheReuseIsVisible:
    """ "My second run finishes immediately" is the cache working correctly.

    Nothing said so. The word "cached" appeared inside a dense totals line on
    a run that was over before anyone read it, which is how correct behaviour
    came to be reported as a bug.
    """

    def test_a_fully_cached_run_says_no_model_was_called(self, tmp_path):
        assert invoke(tmp_path, *run_args(tmp_path)).exit_code == 0
        again = invoke(tmp_path, *run_args(tmp_path))
        assert again.exit_code == 0, again.output
        assert "every cell came from the response cache" in again.output
        assert "--no-cache" in again.output

    def test_a_partly_cached_run_counts_them(self, tmp_path):
        assert invoke(tmp_path, *run_args(tmp_path)).exit_code == 0
        config = CONFIG.replace(
            "  - {id: c2, vars: {q: beta}, expected: beta}\n",
            "  - {id: c2, vars: {q: beta}, expected: beta}\n"
            "  - {id: c3, vars: {q: gamma}, expected: gamma}\n",
        )
        again = invoke(tmp_path, *run_args(tmp_path), config=config)
        assert again.exit_code == 0, again.output
        assert "2 of 3 cells came from the response cache" in again.output

    def test_a_first_run_says_nothing(self, tmp_path):
        result = invoke(tmp_path, *run_args(tmp_path))
        assert "response cache" not in result.output

    def test_no_cache_says_nothing(self, tmp_path):
        assert invoke(tmp_path, *run_args(tmp_path)).exit_code == 0
        again = invoke(tmp_path, *run_args(tmp_path, "--no-cache"))
        assert "response cache" not in again.output
