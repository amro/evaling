import json

from click.testing import CliRunner

from evaling.cli import main

ENV = {"EVALING_USER_CONFIG": "/nonexistent"}


def test_init_scaffolds_runnable_project():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["init"], env=ENV, catch_exceptions=False)
        assert result.exit_code == 0
        for name in ("eval.yaml", "prompts/concise.yaml", "prompts/detailed.yaml", "cases.jsonl"):
            assert name in result.output

        # the scaffold must run out of the box with the mock provider
        run = runner.invoke(main, ["--json", "run"], env=ENV, catch_exceptions=False)
        assert run.exit_code == 0, run.output
        payload = json.loads(run.output)
        assert payload["counts"]["failed"] == 0
        assert payload["gate"]["passed"] is True


def test_init_refuses_overwrite_without_force():
    runner = CliRunner()
    with runner.isolated_filesystem():
        assert runner.invoke(main, ["init"], env=ENV).exit_code == 0
        again = runner.invoke(main, ["init"], env=ENV)
        assert again.exit_code == 2
        assert "refusing to overwrite" in again.output

        forced = runner.invoke(main, ["init", "--force"], env=ENV)
        assert forced.exit_code == 0
