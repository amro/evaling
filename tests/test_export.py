import csv
import io
import json

import pytest

from evaling.engine import run_eval
from evaling.errors import EvalingError
from evaling.export import export_run
from evaling.storage import RunStore
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
