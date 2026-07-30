"""One hostile run, driven through every surface that can display it.

Model output, run labels, case ids, and variant names are all text evaling
does not control, and they flow into rich markup, HTML, CSV, markdown, and
JSON. Every leak and crash of that shape found so far was found separately,
per call site: `show` crashing on a label, `-v show --case` crashing on model
output, `validate` crashing on a variant name, CSV injectable through a case
id, a credential surviving in an error.

So this fixture is the class rather than an instance. One run carrying every
hostile shape at once, then every command and every tool over it, asserting
the same three things each time: it does not crash, nothing is interpreted,
and no secret survives. A new command joins the list and is covered.
"""

import json

import pytest
from click.testing import CliRunner

from evaling.cli import main
from evaling.config import Settings, load_config
from evaling.engine import run_eval
from evaling.export import export_run
from evaling.storage import RunStore

ENV = {"EVALING_USER_CONFIG": "/nonexistent"}

#: Each is a thing that has, or plausibly could, break a surface.
MARKUP = "[/bold]x[red]"  # closing tag with no opener: crashed rich
FORMULA = '=HYPERLINK("http://evil.example","click")'  # spreadsheet injection
HTML = "<script>alert(1)</script>"  # must never be live in a report
MARKDOWN = "| broken | table |"  # must not escape its cell
SECRET = "sk-ant-a-real-looking-live-credential"
UNICODE = (
    "\u00fcn\u00efc\u00f8d\u00e9 \u202e reversed \U0001f642"  # accents, an RTL override, an emoji
)

HOSTILE_OUTPUT = f"{MARKUP} {HTML} {MARKDOWN} {UNICODE} {SECRET}"


@pytest.fixture(scope="module", params=[False, True], ids=["cache-off", "cache-on"])
def hostile(request, tmp_path_factory):
    """A finished run in which every text field is hostile.

    Run once with the response cache off and once with it on, because the
    cache is on by *default* and every other test in the suite disables it for
    speed. That blind spot is not hypothetical: a credential-scrubbing fix
    passed this whole file while leaving the key in `.evaling/cache/`, because
    the cache stores the completion before the record is built.
    """
    path = tmp_path_factory.mktemp("hostile")
    secrets = path / ".evaling.secrets.yaml"
    secrets.write_text(f"MY_KEY: {SECRET}\n", encoding="utf-8")
    # 0600, or doctor reports a world-readable secrets file and exits 1 —
    # correctly, which would make every command below look like a failure.
    secrets.chmod(0o600)
    (path / "eval.yaml").write_text(
        "models:\n"
        "  - id: 'mock'\n"
        "    provider: mock\n"
        f"    params: {{response: '{HOSTILE_OUTPUT}'}}\n"
        "variants:\n"
        f"  - name: 'v{MARKUP}1'\n"
        '    prompt: [{role: user, content: "{{ q }}"}]\n'
        f"cases: [{{id: '{FORMULA}', vars: {{q: a}}}}, {{id: 'c{MARKUP}2', vars: {{q: b}}}}]\n"
        'scorecard: [{criterion: acc, scorer: {type: contains, value: ""}}]\n',
        encoding="utf-8",
    )
    settings = Settings.model_validate(
        {
            "output_dir": str(path / "runs"),
            "cache_dir": str(path / "cache"),
            "cache": request.param,
        }
    )
    config = load_config(path / "eval.yaml")
    result = run_eval(config, settings, label=f"lbl{MARKUP}")
    if request.param:
        # A second pass so a cache *hit* is exercised too — serving from the
        # cache is a different path to the same output.
        result = run_eval(config, settings, label=f"cached{MARKUP}")
        assert result.counts["cached"] == 2, "the cache was not exercised"
    assert result.counts["total"] == 2
    return path, result


def cli(path, *args):
    return CliRunner().invoke(
        main,
        ["-c", str(path / "eval.yaml"), "-o", str(path / "runs"), *args],
        env=ENV,
        catch_exceptions=False,
    )


#: Every command that displays a stored run. A new one belongs here.
READING_COMMANDS = [
    ("list", ["list"]),
    ("show", ["show", "latest"]),
    ("show --failures", ["show", "latest", "--failures"]),
    ("show --case", ["show", "latest", "--case", FORMULA]),
    ("verbose show --case", ["-v", "show", "latest", "--case", FORMULA]),
    ("export json", ["export", "latest", "--format", "json"]),
    ("export csv", ["export", "latest", "--format", "csv"]),
    ("export md", ["export", "latest", "--format", "md"]),
    ("export html", ["export", "latest", "--format", "html"]),
    ("json list", ["--json", "list"]),
    ("json show", ["--json", "show", "latest"]),
    ("doctor", ["doctor"]),
    ("validate", ["validate"]),
]


class TestNoSurfaceCrashes:
    @pytest.mark.parametrize("case", READING_COMMANDS, ids=lambda c: c[0])
    def test_the_command_survives(self, hostile, case):
        path, _ = hostile
        _, args = case
        result = cli(path, *args)
        assert "MarkupError" not in result.output, "rich interpreted content as markup"
        assert "Traceback" not in result.output
        # validate exits 2 only if a prompt fails to render; nothing here does.
        assert result.exit_code == 0, result.output

    def test_the_html_report_renders(self, hostile):
        path, _ = hostile
        out = path / "report.html"
        assert cli(path, "export", "latest", "--format", "html", "--out", str(out)).exit_code == 0
        assert out.read_text(encoding="utf-8")

    def test_compare_survives(self, hostile):
        path, _ = hostile
        cli(path, "run", "--label", "second")
        result = cli(path, "compare", "latest", "second")
        assert result.exit_code == 0, result.output
        assert "MarkupError" not in result.output


class TestNothingIsInterpreted:
    def test_html_output_is_inert(self, hostile):
        path, result = hostile
        store = RunStore(path / "runs")
        html = export_run(store.load_meta(result.run_id), store.load_results(result.run_id), "html")
        assert "<script>" not in html, "a script tag survived into the report"
        assert "&lt;script&gt;" in html

    def test_csv_formulas_are_neutralized(self, hostile):
        import csv as csvlib
        import io

        path, result = hostile
        store = RunStore(path / "runs")
        text = export_run(store.load_meta(result.run_id), store.load_results(result.run_id), "csv")
        for row in csvlib.DictReader(io.StringIO(text, newline="")):
            for column in ("case_id", "variant", "model", "output", "error"):
                value = row.get(column) or ""
                assert value.lstrip()[:1] not in ("=", "+", "@"), f"{column} is a live formula"

    def test_markdown_cells_are_not_broken_out_of(self, hostile):
        path, result = hostile
        store = RunStore(path / "runs")
        text = export_run(store.load_meta(result.run_id), store.load_results(result.run_id), "md")
        # Every table row has the same column count as its header.
        rows = [line for line in text.splitlines() if line.startswith("|")]
        if rows:
            widths = {line.count("|") for line in rows}
            assert len(widths) == 1, f"a value broke the table into widths {widths}"

    def test_json_output_is_parseable(self, hostile):
        path, _ = hostile
        for args in (["--json", "list"], ["--json", "show", "latest"]):
            json.loads(cli(path, *args).output)


class TestNoSecretSurvives:
    """The credential is in the model's output, so it reaches every surface."""

    @pytest.mark.parametrize("case", READING_COMMANDS, ids=lambda c: c[0])
    def test_no_command_prints_it(self, hostile, case):
        path, _ = hostile
        _, args = case
        assert SECRET not in cli(path, *args).output, f"{case[0]} printed the credential"

    def test_nothing_evaling_wrote_carries_it(self, hostile):
        """Runs, cache, artifacts — everything evaling produced.

        Only what evaling *derived*. The fixture's config asks the model to
        return the credential, so `eval.yaml` and the snapshot evaling takes
        of it necessarily contain the value — that is the test's own input,
        and evaling has never claimed to scrub a secret someone typed into a
        config. What must be clean is everything downstream of the model
        call: results, cache, artifacts, metadata.
        """
        path, _ = hostile
        written = [
            file
            for directory in (path / "runs", path / "cache")
            for file in directory.rglob("*")
            if file.is_file() and file.name != "config.snapshot.yaml"
        ]
        assert written, "the run produced no files to check"
        for file in written:
            text = file.read_text(encoding="utf-8", errors="ignore")
            assert SECRET not in text, f"{file.relative_to(path)} carries the credential"

    @pytest.mark.parametrize("fmt", ["json", "csv", "md", "html"])
    def test_no_export_carries_it(self, hostile, fmt):
        path, result = hostile
        store = RunStore(path / "runs")
        text = export_run(store.load_meta(result.run_id), store.load_results(result.run_id), fmt)
        assert SECRET not in text


class TestTheMcpToolsSurviveItToo:
    """The same fixture over the agent-facing surface."""

    def tools(self, path, run_id):
        from evaling.mcp_server import (
            compare_runs_tool,
            get_case_result_tool,
            get_run_tool,
            list_runs_tool,
        )

        out = str(path / "runs")
        return {
            "list_runs": lambda: list_runs_tool(output_dir=out),
            "get_run summary": lambda: get_run_tool(run_id, output_dir=out),
            "get_run failures": lambda: get_run_tool(run_id, "failures", output_dir=out),
            "get_run full": lambda: get_run_tool(run_id, "full", output_dir=out),
            "get_case_result": lambda: get_case_result_tool(
                run_id, f"v{MARKUP}1", "mock", FORMULA, output_dir=out
            ),
            "compare_runs": lambda: compare_runs_tool(run_id, run_id, output_dir=out),
        }

    def test_every_tool_returns_serializable_json(self, hostile):
        path, result = hostile
        for name, call in self.tools(path, result.run_id).items():
            payload = call()
            json.dumps(payload)  # an agent has to be able to read it
            assert payload, name

    def test_no_tool_returns_the_credential(self, hostile):
        path, result = hostile
        for name, call in self.tools(path, result.run_id).items():
            assert SECRET not in json.dumps(call()), f"{name} returned the credential"
