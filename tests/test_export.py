import csv
import io
import json

import pytest

from evaling.engine import run_eval
from evaling.errors import EvalingError
from evaling.export import export_run
from evaling.storage import ResultRecord, RunStore
from helpers import make_config, make_settings


@pytest.fixture
def finished_run(tmp_path):
    config = make_config(
        tmp_path,
        cases=[
            {"id": "c1", "vars": {"q": "alpha"}, "expected": "alpha"},
            {"id": "c2", "vars": {"q": "beta"}, "expected": "WRONG"},
        ],
    )
    config.thresholds.min_pass_rate = 0.9
    settings = make_settings(tmp_path)
    result = run_eval(config, settings, label="export-test")
    store = RunStore(settings.output_dir)
    return store.load_meta(result.run_id), store.load_results(result.run_id)


def test_export_json_roundtrips(finished_run):
    meta, records = finished_run
    data = json.loads(export_run(meta, records, "json"))
    assert data["run"]["id"] == meta["id"]
    assert len(data["results"]) == 2
    assert {r["case_id"] for r in data["results"]} == {"c1", "c2"}
    assert data["results"][0]["scores"]["acc"]["weight"] == 1.0


def test_export_csv_has_cell_and_criterion_columns(finished_run):
    meta, records = finished_run
    rows = list(csv.DictReader(io.StringIO(export_run(meta, records, "csv"))))
    assert len(rows) == 2
    by_case = {row["case_id"]: row for row in rows}
    assert by_case["c1"]["cell_passed"] == "True"
    assert by_case["c1"]["score:acc"] == "1.0"
    assert by_case["c2"]["passed:acc"] == "False"
    assert by_case["c1"]["output"] == "alpha"


def test_export_md_summary_matrix_gate_failures(finished_run):
    meta, records = finished_run
    text = export_run(meta, records, "md")
    assert f"# evaling run `{meta['id']}`" in text
    assert "**Label:** export-test" in text
    assert "pass rate 50.0%" in text
    assert "❌ failed" in text  # gate: min_pass_rate 0.9 not met
    assert "| v1 | m1 |" in text
    assert "## Failures (1)" in text
    assert "v1 × m1 × c2" in text
    assert "expected 'WRONG'" in text


def test_unknown_format_rejected(finished_run):
    meta, records = finished_run
    with pytest.raises(EvalingError, match="unknown export format"):
        export_run(meta, records, "xml")


def test_csv_neutralizes_formula_injection(tmp_path):
    # Regression (CWE-1236): outputs starting with =+-@ open as live formulas
    # in spreadsheets unless prefixed.
    config = make_config(
        tmp_path,
        models=[{"id": "m1", "provider": "mock", "params": {"response": "=1+1"}}],
        cases=[{"id": "c1", "vars": {"q": "x"}, "expected": "=1+1"}],
    )
    settings = make_settings(tmp_path)
    result = run_eval(config, settings)
    store = RunStore(settings.output_dir)
    text = export_run(store.load_meta(result.run_id), store.load_results(result.run_id), "csv")
    row = next(csv.DictReader(io.StringIO(text)))
    assert row["output"] == "'=1+1"


def test_md_sanitizes_untrusted_error_text(tmp_path):
    config = make_config(
        tmp_path,
        models=[{"id": "m1", "provider": "mock", "params": {"response": "nope"}}],
        cases=[{"id": "c1", "vars": {"q": "x"}, "expected": "line1\n<script>[link]"}],
    )
    settings = make_settings(tmp_path)
    result = run_eval(config, settings)
    store = RunStore(settings.output_dir)
    text = export_run(store.load_meta(result.run_id), store.load_results(result.run_id), "md")
    failures_section = text.split("## Failures")[1]
    assert "\\<script>" in failures_section
    assert "\\[link]" in failures_section
    # the multi-line detail stays inside one bullet line
    bullet_lines = [line for line in failures_section.splitlines() if line.startswith("- ")]
    assert len(bullet_lines) == 1


class TestEveryTextCellIsFormulaSafe:
    """CWE-1236 applies to every string a spreadsheet will open, not just output.

    A case id comes from the dataset, which is as external as a model's
    response — and a dataset is exactly the kind of file that arrives from
    somewhere else.
    """

    def record_with(self, **fields):
        record = ResultRecord(
            variant=fields.get("variant", "v"),
            model=fields.get("model", "m"),
            case_id=fields.get("case_id", "c"),
        )
        record.output = fields.get("output", "ok")
        record.scores = {"acc": {"weight": 1.0, "score": 1.0, "passed": True}}
        return record

    def meta(self):
        return {"id": "r1", "status": "complete", "counts": {}, "totals": {}}

    def cell(self, csv_text, column):
        """The parsed value of one column, so CSV quoting isn't in the way."""
        import csv as csvlib
        import io

        [row] = list(csvlib.DictReader(io.StringIO(csv_text, newline="")))
        return row[column]

    @pytest.mark.parametrize("field", ["case_id", "variant", "model"])
    @pytest.mark.parametrize("payload", ['=HYPERLINK("http://evil","x")', "+1", "-1+1", "@SUM(A1)"])
    def test_a_formula_is_neutralized(self, field, payload):
        csv_text = export_run(self.meta(), [self.record_with(**{field: payload})], "csv")
        value = self.cell(csv_text, field)
        # A leading apostrophe, so no spreadsheet evaluates it — and the
        # original text is still readable after it.
        assert value == "'" + payload

    def test_ordinary_values_are_untouched(self):
        csv_text = export_run(self.meta(), [self.record_with(case_id="c-42")], "csv")
        assert self.cell(csv_text, "case_id") == "c-42"
