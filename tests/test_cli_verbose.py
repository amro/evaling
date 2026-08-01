"""`-v`: the prompt, the response, and the scores, as each cell finishes.

Verbose is the only surface that shows what was actually sent to a model — the
tables have no room for it, and it is otherwise readable only as exported JSON.
So the tests here are mostly about what reaches the terminal *verbatim*, and
about the two things that have historically gone wrong with printing model
output: markup in it, and too much of it.
"""

import json
import re

import pytest
from click.testing import CliRunner

from evaling.cli import display, main
from evaling.storage import ResultRecord

ENV = {"EVALING_USER_CONFIG": "/nonexistent"}

CONFIG = """\
models: [{id: mock, provider: mock}]
variants:
  - name: v1
    prompt:
      - {role: system, content: "You are terse."}
      - {role: user, content: "{{ q }}"}
cases:
  - {id: c1, vars: {q: alpha}, expected: alpha}
  - {id: c2, vars: {q: bravo}, expected: bravo}
scorecard: [{criterion: acc, scorer: {type: exact}}]
"""


def invoke(tmp_path, *args, config=CONFIG):
    (tmp_path / "eval.yaml").write_text(config, encoding="utf-8")
    base = [
        "-c",
        str(tmp_path / "eval.yaml"),
        "-o",
        str(tmp_path / "runs"),
        "--cache-dir",
        str(tmp_path / "cache"),
    ]
    return CliRunner().invoke(main, [*base, *args], env=ENV, catch_exceptions=False)


def flat(output: str) -> str:
    """Output with rich's wrapping and gutter alignment collapsed away."""
    return re.sub(r"\s+", " ", output)


class TestAVerboseRun:
    def test_it_prints_the_prompt_the_response_and_the_scores(self, tmp_path):
        result = invoke(tmp_path, "-v", "run")
        assert result.exit_code == 0, result.output
        text = flat(result.output)

        assert "system │ You are terse." in text  # the prompt evaling actually sent
        assert "user │ alpha" in text
        assert "output │ alpha" in text  # the mock echoes it back
        assert "acc 1.000 pass" in text
        assert "v1 × mock × c1" in text

    def test_quiet_by_default(self, tmp_path):
        result = invoke(tmp_path, "run")
        assert result.exit_code == 0, result.output
        assert "You are terse." not in result.output
        assert "scores │" not in result.output

    def test_json_output_stays_machine_readable(self, tmp_path):
        """`--json` promises one JSON document on stdout; `-v` must not spoil it."""
        result = invoke(tmp_path, "-v", "--json", "run")
        assert result.exit_code == 0, result.output
        assert "scores │" not in result.output
        json.loads(result.output)  # raises if the block leaked into stdout

    def test_a_failing_cell_says_so_and_names_the_criterion(self, tmp_path):
        config = CONFIG.replace("expected: alpha", "expected: not-alpha")
        result = invoke(tmp_path, "-v", "run", config=config)
        assert result.exit_code == 0, result.output  # no thresholds, so no gate
        text = flat(result.output)
        assert "FAIL" in text
        assert "acc 0.000 fail" in text
        assert "expected 'not-alpha'" in text

    def test_an_errored_cell_shows_the_error_instead_of_an_output(self, tmp_path):
        config = CONFIG.replace(
            "models: [{id: mock, provider: mock}]",
            "models: [{id: mock, provider: mock, params: {error: fatal}}]",
        )
        result = invoke(tmp_path, "-v", "run", config=config)
        text = flat(result.output)
        assert "ERROR" in text
        assert "error │" in text
        assert "mock fatal error" in text
        assert "output │" not in text

    def test_a_cached_cell_is_labelled_cached(self, tmp_path):
        """The whole complaint about the cache is that reuse is invisible."""
        assert invoke(tmp_path, "run").exit_code == 0
        again = invoke(tmp_path, "-v", "run")
        assert again.exit_code == 0, again.output
        assert "cached" in flat(again.output)


class TestUntrustedOutput:
    HOSTILE = "[/bold]danger[red]"

    def hostile_config(self, response):
        return (
            "models:\n  - id: mock\n    provider: mock\n"
            f"    params: {{response: {response!r}}}\n"
            "variants:\n  - name: v1\n"
            '    prompt: [{role: user, content: "{{ q }}"}]\n'
            "cases: [{id: c1, vars: {q: alpha}}]\n"
            'scorecard: [{criterion: acc, scorer: {type: contains, value: ""}}]\n'
        )

    def test_markup_in_a_response_neither_crashes_nor_styles(self, tmp_path):
        result = invoke(tmp_path, "-v", "run", config=self.hostile_config(self.HOSTILE))
        assert result.exit_code == 0, result.output
        # Printed as characters, not interpreted as a tag.
        assert "danger" in flat(result.output)

    def test_markup_in_a_case_id_or_variant_name_is_safe(self, tmp_path):
        config = (
            "models: [{id: mock, provider: mock}]\n"
            "variants:\n  - name: '[/bold]v[red]'\n"
            '    prompt: [{role: user, content: "{{ q }}"}]\n'
            "cases: [{id: '[bold]c1', vars: {q: alpha}}]\n"
            'scorecard: [{criterion: acc, scorer: {type: contains, value: ""}}]\n'
        )
        result = invoke(tmp_path, "-v", "run", config=config)
        assert result.exit_code == 0, result.output


class TestBounding:
    def response_config(self, response, prompt="{{ q }}"):
        """JSON, which YAML accepts — a newline in a YAML single-quoted scalar
        is the two characters backslash-n, which made the first version of
        these tests assert nothing."""
        return json.dumps(
            {
                "models": [{"id": "mock", "provider": "mock", "params": {"response": response}}],
                "variants": [{"name": "v1", "prompt": [{"role": "user", "content": prompt}]}],
                "cases": [{"id": "c1", "vars": {"q": "alpha"}}],
                "scorecard": [{"criterion": "acc", "scorer": {"type": "contains", "value": ""}}],
            }
        )

    def test_a_runaway_response_is_bounded_by_lines(self, tmp_path):
        response = "\n".join(f"line{i}" for i in range(display.OUTPUT_LINES + 50))
        result = invoke(tmp_path, "-v", "run", config=self.response_config(response))
        assert result.exit_code == 0, result.output
        assert "line0" in result.output
        assert "line249" not in result.output
        assert "50 more lines" in flat(result.output)

    def test_a_runaway_response_is_bounded_by_length(self, tmp_path):
        """A response with no newlines is one line however long it is."""
        response = "x" * (display.OUTPUT_CHARS + 5_000)
        result = invoke(tmp_path, "-v", "run", config=self.response_config(response))
        assert result.exit_code == 0, result.output
        assert f"truncated at {display.OUTPUT_CHARS} characters" in flat(result.output)

    def test_a_long_prompt_is_bounded_more_tightly_than_a_response(self, tmp_path):
        """The prompt repeats on every cell; the response is what you came for."""
        assert display.PROMPT_LINES < display.OUTPUT_LINES
        long_prompt = "\n".join(f"p{i}" for i in range(display.PROMPT_LINES + 10))
        config = self.response_config("short answer", prompt=long_prompt)
        result = invoke(tmp_path, "-v", "run", config=config)
        assert result.exit_code == 0, result.output
        assert "10 more lines" in flat(result.output)


class TestBlocksStayWhole:
    """A cell's lines must not be interleaved with another cell's.

    rich holds its lock for the duration of one `print`, so a block emitted as
    a single renderable survives concurrency and a block emitted as several
    prints does not. This is the test that fails if someone splits it up.
    """

    def test_cell_block_is_one_renderable(self):
        record = ResultRecord(variant="v", model="m", case_id="c", output="hi")
        block = display.cell_block(record)
        assert hasattr(block, "renderables")  # a Group: one print, one lock

    @pytest.mark.parametrize("concurrency", ["1", "8"])
    def test_each_case_appears_under_its_own_header(self, tmp_path, concurrency):
        cases = ", ".join(f"{{id: c{i}, vars: {{q: word{i}}}}}" for i in range(8))
        config = (
            "models: [{id: mock, provider: mock}]\n"
            "variants:\n  - name: v1\n"
            '    prompt: [{role: user, content: "{{ q }}"}]\n'
            f"cases: [{cases}]\n"
            'scorecard: [{criterion: acc, scorer: {type: contains, value: ""}}]\n'
        )
        result = invoke(tmp_path, "-v", "run", "--concurrency", concurrency, config=config)
        assert result.exit_code == 0, result.output

        # Split the output on headers; each block must mention its own case's
        # word and no other block's.
        blocks = re.split(r"v1 × mock × ", flat(result.output))[1:]
        assert len(blocks) == 8
        for block in blocks:
            case_id = block.split()[0]
            index = case_id.removeprefix("c")
            assert f"word{index}" in block, block
            others = {f"word{i}" for i in range(8)} - {f"word{index}"}
            assert not (others & set(block.split())), block


class TestTheCaseDrilldown:
    def test_it_shows_the_prompt_too(self, tmp_path):
        """`show --case -v` and a verbose run print the same block.

        Before this, the rendered prompt had no human-readable surface at all:
        it was stored on every record and reachable only through
        `export --format json`.
        """
        assert invoke(tmp_path, "run").exit_code == 0
        result = invoke(tmp_path, "-v", "show", "latest", "--case", "c1")
        assert result.exit_code == 0, result.output
        text = flat(result.output)
        assert "system │ You are terse." in text
        assert "output │ alpha" in text
        assert "acc 1.000 pass" in text

    def test_without_verbose_it_stays_a_table(self, tmp_path):
        assert invoke(tmp_path, "run").exit_code == 0
        result = invoke(tmp_path, "show", "latest", "--case", "c1")
        assert result.exit_code == 0, result.output
        assert "You are terse." not in result.output
