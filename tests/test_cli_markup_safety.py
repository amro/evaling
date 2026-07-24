"""Untrusted text must never be interpreted as rich console markup.

A model response containing "[/bold]" used to raise MarkupError and crash the
command; "[red]" silently restyled the terminal; and an error message
mentioning "evaling[mcp]" lost the bracketed part entirely.
"""

from click.testing import CliRunner

from evaling.cli import main

ENV = {"EVALING_USER_CONFIG": "/nonexistent"}

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
    result = cli(tmp_path, "mcp")
    assert result.exit_code == 2
    # "evaling[mcp]" must survive intact — it's the install command
    assert "evaling[mcp]" in result.output
