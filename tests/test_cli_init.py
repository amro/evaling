import json
import re

from click.testing import CliRunner

from evaling.cli import main
from evaling.cli.scaffold import CASES_JSONL

ENV = {"EVALING_USER_CONFIG": "/nonexistent"}


def flat(output: str) -> str:
    """Output with rich's wrapping collapsed, so a match cannot fall on a break."""
    return re.sub(r"\s+", " ", output)


def test_init_scaffolds_runnable_project(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
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


def test_init_refuses_overwrite_without_force(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(main, ["init"], env=ENV).exit_code == 0
    again = runner.invoke(main, ["init"], env=ENV)
    assert again.exit_code == 2
    assert "refusing to overwrite" in again.output

    forced = runner.invoke(main, ["init", "--force"], env=ENV)
    assert forced.exit_code == 0


def test_force_keeps_an_existing_gitignores_entries(tmp_path, monkeypatch):
    """A .gitignore usually predates evaling and covers the rest of the repo.

    Every other scaffold file belongs to evaling, so replacing it is what
    --force means. Replacing this one dropped the user's entries.
    """
    from pathlib import Path

    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    Path(".gitignore").write_text("node_modules/\n*.log\n", encoding="utf-8")
    assert runner.invoke(main, ["init", "--force"], env=ENV).exit_code == 0
    gitignore = Path(".gitignore").read_text(encoding="utf-8")
    assert "node_modules/" in gitignore, "the user's own entries were discarded"
    assert "*.log" in gitignore
    assert ".evaling/" in gitignore
    assert ".evaling.secrets.yaml" in gitignore


def test_force_does_not_duplicate_entries_it_already_added(tmp_path, monkeypatch):
    from pathlib import Path

    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(main, ["init"], env=ENV).exit_code == 0
    assert runner.invoke(main, ["init", "--force"], env=ENV).exit_code == 0
    gitignore = Path(".gitignore").read_text(encoding="utf-8")
    assert gitignore.count(".evaling.secrets.yaml") == 1


def test_the_gitignore_action_is_reported_accurately(tmp_path, monkeypatch):
    """ "created" was printed even when the file was merged or untouched."""
    from pathlib import Path

    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    Path(".gitignore").write_text("node_modules/\n", encoding="utf-8")
    merged = runner.invoke(main, ["init", "--force"], env=ENV)
    assert "updated .gitignore" in flat(merged.output), merged.output

    again = runner.invoke(main, ["init", "--force"], env=ENV)
    assert "left alone .gitignore" in flat(again.output), again.output


def test_a_gitignore_left_alone_is_not_rewritten(tmp_path, monkeypatch):
    """CRLF made "left alone" false: reading translates newlines and writing
    with "\n" converted every line, so git showed the file modified."""
    from pathlib import Path

    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    path = Path(".gitignore")
    assert runner.invoke(main, ["init"], env=ENV).exit_code == 0
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    before = path.read_bytes()

    result = runner.invoke(main, ["init", "--force"], env=ENV)
    assert "left alone .gitignore" in flat(result.output), result.output
    assert path.read_bytes() == before, "the file was rewritten despite the label"


def test_merging_into_an_empty_gitignore_adds_the_entries(tmp_path, monkeypatch):
    from pathlib import Path

    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    Path(".gitignore").write_text("   \n\n", encoding="utf-8")
    assert runner.invoke(main, ["init", "--force"], env=ENV).exit_code == 0
    gitignore = Path(".gitignore").read_text(encoding="utf-8")
    assert gitignore.startswith("#"), "a leading blank line was left behind"
    assert ".evaling/" in gitignore and ".evaling.secrets.yaml" in gitignore


def test_a_gitignore_that_is_not_utf8_is_refused_before_anything_is_written(tmp_path, monkeypatch):
    """It raised mid-loop, after eval.yaml had already been replaced.

    A traceback and a half-written scaffold, from a file evaling only wanted
    to append two lines to.
    """
    from pathlib import Path

    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    Path(".gitignore").write_bytes(b"# caf\xe9\nnode_modules/\n")
    result = runner.invoke(main, ["init", "--force"], env=ENV)
    assert result.exit_code == 2, result.output
    # Collapsed: rich wraps to the terminal width, and where the break
    # lands depends on the tmp path's length — on Windows it fell between
    # "valid" and "UTF-8" and split the phrase being matched.
    assert "not valid UTF-8" in flat(result.output), result.output
    assert not Path("eval.yaml").exists(), "the scaffold was left half-written"


def test_merging_into_a_crlf_gitignore_keeps_crlf(tmp_path, monkeypatch):
    """Appending LF into a CRLF file leaves it mixed; autocrlf setups warn."""
    from pathlib import Path

    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    path = Path(".gitignore")
    path.write_bytes(b"node_modules/\r\nlogs/\r\n")
    assert runner.invoke(main, ["init", "--force"], env=ENV).exit_code == 0
    raw = path.read_bytes()
    assert raw.count(b"\n") == raw.count(b"\r\n"), "the merge left mixed line endings"
    assert b".evaling.secrets.yaml" in raw

    # And the second run recognises its own work.
    before = path.read_bytes()
    again = runner.invoke(main, ["init", "--force"], env=ENV)
    assert "left alone .gitignore" in flat(again.output)
    assert path.read_bytes() == before
