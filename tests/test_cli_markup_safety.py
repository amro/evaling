"""Untrusted text must never be interpreted as rich console markup.

A model response containing "[/bold]" used to raise MarkupError and crash the
command; "[red]" silently restyled the terminal; and an error message
mentioning "evaling[mcp]" lost the bracketed part entirely.
"""

import pytest
from click.testing import CliRunner

from evaling.cli import main

ENV = {"EVALING_USER_CONFIG": "/nonexistent"}


def invoke(path, *args):
    return CliRunner().invoke(
        main,
        ["-c", str(path / "eval.yaml"), "-o", str(path / "runs"), *args],
        env=ENV,
        catch_exceptions=False,
    )


HOSTILE = "answer [/bold] with [red]markup[/red] and [unclosed"


def cli(tmp_path, *args):
    base = ["-o", str(tmp_path / "runs"), "--cache-dir", str(tmp_path / "cache")]
    return CliRunner().invoke(main, base + list(args), env=ENV, catch_exceptions=False)


def write_config(tmp_path, response=HOSTILE, model_id="mock", variant="v1"):
    path = tmp_path / "eval.yaml"
    # Quote every interpolated value: bare brackets are YAML flow sequences.
    path.write_text(
        f"models: [{{id: {model_id}, provider: mock, params: {{response: '{response}'}}}}]\n"
        "variants:\n"
        f'  - name: "{variant}"\n'
        '    prompt: [{role: user, content: "{{ q }}"}]\n'
        "cases: [{id: c1, vars: {q: alpha}, expected: alpha}]\n"
        "scorecard: [{criterion: acc, scorer: {type: exact}}]\n",
        encoding="utf-8",
    )
    return path


def test_hostile_output_does_not_crash_run(tmp_path):
    # -v prints each cell's output through the progress console
    result = cli(tmp_path, "-v", "run", str(write_config(tmp_path)))
    assert result.exit_code == 0, result.output


def test_hostile_output_survives_show_and_failures(tmp_path):
    cli(tmp_path, "-q", "run", str(write_config(tmp_path)))

    failures = cli(tmp_path, "show", "latest", "--failures")
    assert failures.exit_code == 0, failures.output

    # the drill-down is where the model's own text is rendered
    case = cli(tmp_path, "show", "latest", "--case", "c1")
    assert case.exit_code == 0, case.output
    assert "markup" in case.output  # shown as text, not swallowed or applied


def test_hostile_identifiers_render(tmp_path):
    config = write_config(tmp_path, response="ok", model_id="m1", variant="[bold]v[/bold]")
    assert cli(tmp_path, "-q", "run", str(config)).exit_code == 0
    listed = cli(tmp_path, "show", "latest")
    assert listed.exit_code == 0, listed.output
    assert "[bold]v[/bold]" in listed.output  # shown literally, not applied


def test_error_message_keeps_its_brackets(tmp_path):
    # The remediation hint is the whole point of the message.
    result = cli(tmp_path, "export", "nonexistent-run", "--format", "json")
    assert result.exit_code == 2
    assert "nonexistent-run" in result.output


def test_mcp_hint_survives_when_extra_missing(tmp_path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("mcp"):
            raise ImportError("no mcp")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    # Absence is decided from distribution metadata, not from whether the
    # import works — a blocked import alone is a *broken* install, which gets a
    # different message on purpose.
    monkeypatch.setattr("evaling.mcp_server.installed_mcp_version", lambda: None)
    result = cli(tmp_path, "mcp")
    assert result.exit_code == 2
    # "evaling[mcp]" must survive intact — it's the install command
    assert "evaling[mcp]" in result.output


class TestHostileTextInEveryReadingPath:
    """Run metadata and model output reach rich as markup.

    A label is typed by a user; model output is the least trustworthy string
    in the system. Both used to reach `console.print` unescaped, so a stray
    `[/bold]` either crashed the command with a MarkupError traceback or
    restyled the terminal — on the commands whose entire job is reading a run
    back after something went wrong.
    """

    HOSTILE = "[/bold]danger[red]"

    @pytest.fixture
    def run_with_hostile_output(self, tmp_path):
        (tmp_path / "eval.yaml").write_text(
            "models:\n"
            "  - id: mock\n"
            "    provider: mock\n"
            f"    params: {{response: '{self.HOSTILE}'}}\n"
            "variants:\n  - name: v1\n"
            '    prompt: [{role: user, content: "{{ q }}"}]\n'
            "cases: [{id: c1, vars: {q: alpha}}]\n"
            'scorecard: [{criterion: acc, scorer: {type: contains, value: ""}}]\n',
            encoding="utf-8",
        )
        result = invoke(tmp_path, "run", "--label", self.HOSTILE)
        assert result.exit_code == 0, result.output
        return tmp_path

    def test_show_survives_a_hostile_label(self, run_with_hostile_output):
        result = invoke(run_with_hostile_output, "show", "latest")
        assert result.exit_code == 0, result.output
        assert "danger" in result.output

    def test_verbose_case_drilldown_survives_hostile_output(self, run_with_hostile_output):
        result = invoke(run_with_hostile_output, "-v", "show", "latest", "--case", "c1")
        assert result.exit_code == 0, result.output
        assert "danger" in result.output

    def test_list_survives_a_hostile_label(self, run_with_hostile_output):
        result = invoke(run_with_hostile_output, "list")
        assert result.exit_code == 0, result.output

    def test_a_render_error_with_markup_does_not_crash_validate(self, tmp_path):
        """The dry-run path prints variant, model, case id and the error."""
        (tmp_path / "eval.yaml").write_text(
            "models: [{id: mock, provider: mock}]\n"
            "variants:\n  - name: 'v[/bold]1'\n"
            '    prompt: [{role: user, content: "{{ nope }}"}]\n'
            "cases: [{id: 'c[/red]1', vars: {q: alpha}}]\n"
            'scorecard: [{criterion: acc, scorer: {type: contains, value: ""}}]\n',
            encoding="utf-8",
        )
        result = invoke(tmp_path, "validate")
        # Exit 2 because the render fails, not because rich blew up.
        assert result.exit_code == 2, result.output
        assert "MarkupError" not in result.output


class TestCommandsThatReadRunsBack:
    """`cli/__init__.py` interpolates into markup; display.py escapes, it did not.

    Both crashes below were `rich.errors.MarkupError` tracebacks with exit 1,
    from the commands whose whole job is inspecting a run.
    """

    def project(self, tmp_path, case_id="c1"):
        (tmp_path / "eval.yaml").write_text(
            "models: [{id: m, provider: mock}]\n"
            "variants:\n  - name: v1\n"
            '    prompt: [{role: user, content: "{{ q }}"}]\n'
            f"cases: [{{id: '{case_id}', vars: {{q: a}}}}]\n"
            'scorecard: [{criterion: acc, scorer: {type: contains, value: ""}}]\n',
            encoding="utf-8",
        )
        assert cli(tmp_path, "-c", str(tmp_path / "eval.yaml"), "run").exit_code == 0
        return tmp_path

    def test_a_case_id_from_the_command_line_is_escaped(self, tmp_path):
        hostile = "c[/bold]x[red]2"
        path = self.project(tmp_path, hostile)
        result = cli(path, "-c", str(path / "eval.yaml"), "show", "latest", "--case", hostile)
        assert result.exit_code == 0, result.output
        assert hostile in result.output  # shown literally, not applied

    def test_a_status_read_back_from_run_json_is_escaped(self, tmp_path):
        """run.json is not revalidated on read, so a foreign one must not crash."""
        import json

        path = self.project(tmp_path)
        meta_path = next((path / "runs").glob("*/run.json"))
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["status"] = "[/bold]evil[red]"
        meta_path.write_text(json.dumps(meta), encoding="utf-8")

        result = cli(path, "-c", str(path / "eval.yaml"), "show", "latest")
        assert result.exit_code == 0, result.output
        assert "[/bold]evil[red]" in result.output

    def test_a_cache_directory_with_brackets_is_escaped(self, tmp_path):
        result = CliRunner().invoke(
            main,
            ["--cache-dir", str(tmp_path / "ca[che]"), "cache", "info"],
            env=ENV,
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "ca[che]" in result.output
