"""`--fail-fast`: stop at the first failing cell instead of paying for the rest.

For CI, where the whole matrix is a bill and the first failure is the answer.
The behaviour that matters is that stopping is *graceful*: cells already in
flight finish and are recorded, the run finalizes, and what did run is
readable afterwards. Raising instead would throw away a run that had already
been paid for, in order to report the failure it was asked to find.
"""

import json

import pytest
from click.testing import CliRunner

from evaling.cli import main
from evaling.config import Settings, load_config
from evaling.engine import run_eval
from evaling.storage import RunStore

ENV = {"EVALING_USER_CONFIG": "/nonexistent"}

CASES = 30


def config_text(*, failing_from=0):
    """Cases c0..c29; those at or after `failing_from` cannot pass."""
    cases = ", ".join(
        f"{{id: c{i}, vars: {{q: '{i}'}}, expected: "
        + ("NEVER" if i >= failing_from else f"'{i}'")
        + "}"
        for i in range(CASES)
    )
    return (
        "models: [{id: mock, provider: mock}]\n"
        "variants:\n  - name: v1\n"
        '    prompt: [{role: user, content: "{{ q }}"}]\n'
        f"cases: [{cases}]\n"
        "scorecard: [{criterion: acc, scorer: {type: exact}}]\n"
    )


@pytest.fixture
def project(tmp_path):
    def build(*, failing_from=0):
        (tmp_path / "eval.yaml").write_text(
            config_text(failing_from=failing_from), encoding="utf-8"
        )
        return tmp_path

    return build


def settings_for(path, concurrency=4):
    return Settings.model_validate(
        {
            "output_dir": str(path / "runs"),
            "cache_dir": str(path / "cache"),
            "cache": False,
            "concurrency": concurrency,
        }
    )


def invoke(path, *args):
    return CliRunner().invoke(
        main,
        [
            "-c",
            str(path / "eval.yaml"),
            "-o",
            str(path / "runs"),
            "--cache-dir",
            str(path / "c"),
            *args,
        ],
        env=ENV,
        catch_exceptions=False,
    )


class TestTheEngineStops:
    def test_a_failure_stops_the_rest(self, project):
        path = project(failing_from=0)
        result = run_eval(load_config(path / "eval.yaml"), settings_for(path), fail_fast=True)

        assert result.stopped_early is True
        assert result.counts["total"] < CASES, "the whole matrix ran anyway"
        assert result.counts["total"] >= 1

    def test_cells_in_flight_are_still_recorded(self, project):
        """Graceful, not cancelled: whatever was already running is kept."""
        path = project(failing_from=0)
        result = run_eval(
            load_config(path / "eval.yaml"), settings_for(path, concurrency=4), fail_fast=True
        )

        on_disk = RunStore(path / "runs").load_results(result.run_id)
        assert len(on_disk) == result.counts["total"]
        assert result.aggregates["overall"]["cases"] == result.counts["total"]

    def test_the_run_is_finalized_not_abandoned(self, project):
        path = project(failing_from=0)
        result = run_eval(load_config(path / "eval.yaml"), settings_for(path), fail_fast=True)

        meta = RunStore(path / "runs").load_meta(result.run_id)
        assert meta["status"] == "complete"
        assert meta["stopped_early"] is True
        assert meta["counts"] == result.counts

    def test_a_clean_run_is_unaffected(self, project):
        """No failure, no early stop — the flag must cost nothing otherwise."""
        path = project(failing_from=CASES)
        result = run_eval(load_config(path / "eval.yaml"), settings_for(path), fail_fast=True)

        assert result.stopped_early is False
        assert result.counts["total"] == CASES
        assert not [w for w in result.warnings if "fail-fast" in w]

    def test_without_the_flag_everything_runs(self, project):
        path = project(failing_from=0)
        result = run_eval(load_config(path / "eval.yaml"), settings_for(path))

        assert result.stopped_early is False
        assert result.counts["total"] == CASES

    def test_it_stops_near_the_failure_not_at_the_end(self, project):
        """A late failure still leaves most of the matrix unrun."""
        path = project(failing_from=1)  # c0 passes, everything after fails
        result = run_eval(
            load_config(path / "eval.yaml"), settings_for(path, concurrency=2), fail_fast=True
        )

        assert result.stopped_early is True
        # Two workers, so at most a couple more cells land after the first
        # failure is seen. Generous, but nowhere near 30.
        assert result.counts["total"] <= 6, f"{result.counts['total']} of {CASES} cells ran"

    def test_the_warning_says_what_happened(self, project):
        path = project(failing_from=0)
        result = run_eval(load_config(path / "eval.yaml"), settings_for(path), fail_fast=True)
        assert any("stopped early" in warning for warning in result.warnings)


class TestThroughTheCli:
    def test_it_exits_non_zero_with_no_gate_configured(self, project):
        """A build that ended early but exited 0 would read as a pass."""
        path = project(failing_from=0)
        result = invoke(path, "run", "--fail-fast")
        assert result.exit_code == 1, result.output
        assert "stopped early" in result.output

    def test_a_clean_run_still_exits_zero(self, project):
        path = project(failing_from=CASES)
        result = invoke(path, "run", "--fail-fast")
        assert result.exit_code == 0, result.output

    def test_json_output_says_so(self, project):
        path = project(failing_from=0)
        result = invoke(path, "--json", "run", "--fail-fast")
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["stopped_early"] is True
        assert payload["counts"]["total"] < CASES

    def test_quiet_mode_still_says_why(self, project):
        path = project(failing_from=0)
        result = invoke(path, "--quiet", "run", "--fail-fast")
        assert result.exit_code == 1
        assert "stopped early" in result.output


class TestOverMcp:
    def test_the_summary_flags_it(self, project):
        import asyncio

        from evaling.mcp_server import run_eval_tool

        path = project(failing_from=0)
        summary = asyncio.run(
            run_eval_tool(
                config_path=str(path / "eval.yaml"),
                output_dir=str(path / "runs"),
                fail_fast=True,
            )
        )
        assert summary["stopped_early"] is True
        assert summary["counts"]["total"] < CASES

    def test_a_clean_run_does_not_flag_it(self, project):
        import asyncio

        from evaling.mcp_server import run_eval_tool

        path = project(failing_from=CASES)
        summary = asyncio.run(
            run_eval_tool(
                config_path=str(path / "eval.yaml"),
                output_dir=str(path / "runs"),
                fail_fast=True,
            )
        )
        assert "stopped_early" not in summary
