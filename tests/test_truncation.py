"""A response cut off at the output cap is recorded as truncated, not as a success.

Observed live: Claude Haiku 5.5 spent a 200-token ``max_tokens`` entirely on
thinking and returned no text. The API said ``stop_reason: max_tokens``; the
cell was stored as succeeded with an empty output and a score of 0, which is
indistinguishable from a model that answered badly.
"""

import csv
import io
import json

import pytest
from click.testing import CliRunner

from evaling.cli import main
from evaling.engine import run_eval
from evaling.export import export_run
from evaling.mcp_server import get_run_tool
from evaling.providers import _REGISTRY
from evaling.providers.base import Completion
from evaling.providers.mock import MockProvider
from evaling.report import render_run_html
from evaling.storage import RunStore, record_from_dict
from helpers import make_config, make_settings

ENV = {"EVALING_USER_CONFIG": "/nonexistent"}

CONFIG = """\
models: [{id: mock, provider: mock}]
variants:
  - name: v1
    prompt: [{role: user, content: "{{ q }}"}]
cases:
  - {id: c1, vars: {q: alpha}, expected: alpha}
scorecard: [{criterion: acc, scorer: {type: exact}}]
"""


class CutOff(MockProvider):
    """Answers like a model whose output cap ran out before any text."""

    async def complete(self, request):
        return Completion(text="", input_tokens=92, output_tokens=200, truncated=True)


@pytest.fixture
def cut_off(monkeypatch):
    monkeypatch.setitem(_REGISTRY, "mock", CutOff)


@pytest.fixture
def finished(tmp_path, cut_off):
    config = make_config(
        tmp_path, cases=[{"id": "c1", "vars": {"q": "alpha"}, "expected": "alpha"}]
    )
    settings = make_settings(tmp_path)
    result = run_eval(config, settings, label="cap-test")
    store = RunStore(settings.output_dir)
    return store.load_meta(result.run_id), store.load_results(result.run_id)


class TestRecord:
    def test_the_flag_is_persisted_with_the_cell(self, finished):
        _, records = finished
        assert [r.truncated for r in records] == [True]

    def test_an_untruncated_cell_says_so(self, tmp_path):
        result = run_eval(make_config(tmp_path), make_settings(tmp_path))
        records = RunStore(make_settings(tmp_path).output_dir).load_results(result.run_id)
        assert all(r.truncated is False for r in records)

    def test_rows_written_before_the_field_existed_load_as_not_truncated(self):
        record = record_from_dict({"variant": "v", "model": "m", "case_id": "c", "output": "x"})
        assert record.truncated is False

    def test_the_run_counts_truncated_cells(self, finished):
        meta, _ = finished
        assert meta["counts"]["truncated"] == 1
        # Still a completed call, so it is not a failure in the error sense.
        assert meta["counts"]["succeeded"] == 1
        assert meta["counts"]["failed"] == 0


class TestSurfaces:
    def test_show_names_the_truncation(self, tmp_path, cut_off):
        (tmp_path / "eval.yaml").write_text(CONFIG, encoding="utf-8")
        base = ["-o", str(tmp_path / "runs"), "--cache-dir", str(tmp_path / "cache")]
        runner = CliRunner()
        run = runner.invoke(
            main, base + ["run", str(tmp_path / "eval.yaml")], env=ENV, catch_exceptions=False
        )
        assert "1 truncated" in run.output
        shown = runner.invoke(main, base + ["show", "latest"], env=ENV, catch_exceptions=False)
        assert "1 truncated" in shown.output
        failures = runner.invoke(
            main, base + ["show", "latest", "--failures"], env=ENV, catch_exceptions=False
        )
        assert "truncated at max_tokens" in failures.output
        cell = runner.invoke(
            main, base + ["show", "latest", "--case", "c1"], env=ENV, catch_exceptions=False
        )
        assert "truncated at max_tokens" in cell.output

    def test_csv_export_has_a_truncated_column(self, finished):
        meta, records = finished
        rows = list(csv.DictReader(io.StringIO(export_run(meta, records, "csv"))))
        assert rows[0]["truncated"] == "True"

    def test_markdown_export_names_the_truncation(self, finished):
        meta, records = finished
        assert "truncated at max_tokens" in export_run(meta, records, "md")

    def test_json_export_carries_the_flag(self, finished):
        meta, records = finished
        data = json.loads(export_run(meta, records, "json"))
        assert data["results"][0]["truncated"] is True

    def test_html_report_names_the_truncation(self, finished):
        meta, records = finished
        html = render_run_html(meta, records)
        assert "truncated at max_tokens" in html

    def test_mcp_rows_carry_the_flag(self, tmp_path, cut_off):
        (tmp_path / "eval.yaml").write_text(CONFIG, encoding="utf-8")
        base = ["-o", str(tmp_path / "runs"), "--cache-dir", str(tmp_path / "cache")]
        CliRunner().invoke(
            main, base + ["run", str(tmp_path / "eval.yaml")], env=ENV, catch_exceptions=False
        )
        out = get_run_tool("latest", detail="failures", output_dir=str(tmp_path / "runs"))
        assert out["cells"][0]["truncated"] is True
        assert out["counts"]["truncated"] == 1
