import json

from click.testing import CliRunner

from evaling.cli import main
from evaling.cli.scaffold import CASES_JSONL

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


def scaffold_cases():
    return [json.loads(line) for line in CASES_JSONL.splitlines() if line.strip()]


def test_scaffold_cases_are_answerable_offline():
    """Every `expected` must be a literal span of its own transaction.

    The mock provider echoes the prompt rather than answering it, so this is
    what makes `init` && `run` pass with no API key. A case that fails this
    passes only once a real model is wired up, which makes the first run of a
    fresh scaffold report a failure that is not the user's.
    """
    cases = scaffold_cases()
    assert cases
    for case in cases:
        assert case["expected"] in case["transaction"], case["id"]


def test_scaffold_cases_discriminate():
    """No `expected` may appear in another case's transaction.

    Without this the scorecard is satisfied by any output that quotes the
    prompt, which is exactly what the previous scaffold did — its `expected`
    values were words common to the questions, so the criterion measured
    nothing and taught the reader that `expected` need not be the answer.
    """
    cases = scaffold_cases()
    for case in cases:
        for other in cases:
            if other["id"] != case["id"]:
                assert case["expected"] not in other["transaction"], (case["id"], other["id"])


def test_init_refuses_overwrite_without_force():
    runner = CliRunner()
    with runner.isolated_filesystem():
        assert runner.invoke(main, ["init"], env=ENV).exit_code == 0
        again = runner.invoke(main, ["init"], env=ENV)
        assert again.exit_code == 2
        assert "refusing to overwrite" in again.output

        forced = runner.invoke(main, ["init", "--force"], env=ENV)
        assert forced.exit_code == 0
