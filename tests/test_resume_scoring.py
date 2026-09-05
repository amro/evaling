"""A resume must not combine results from different scoring rules."""

import pytest

from evaling.config import EvalConfig
from evaling.engine import config_fingerprint, run_eval
from evaling.storage import StorageError
from helpers import make_settings


@pytest.fixture(params=["python", "json-schema"])
def scoring_project(tmp_path, request):
    if request.param == "python":
        path = tmp_path / "score.py"
        original = "def score(output, case):\n    return True\n"
        changed = "def score(output, case):\n    return False\n"
        scorer = {"type": "python", "file": path.name}
    else:
        path = tmp_path / "schema.json"
        original = '{"type":"string"}'
        changed = '{"type":"number"}'
        scorer = {"type": "json-schema", "schema": path.name}
    path.write_text(original, encoding="utf-8")
    config = EvalConfig.model_validate(
        {
            "models": [
                {"id": "mock", "provider": "mock", "params": {"response": '"ok"', "cost": 1}}
            ],
            "variants": [{"name": "v", "prompt": [{"role": "user", "content": "hello"}]}],
            "cases": [{"id": "a"}, {"id": "b"}],
            "scorecard": [{"criterion": "grade", "scorer": scorer}],
        }
    )
    config._base_dir = tmp_path
    return config, path, original, changed


def test_changed_scoring_file_refuses_resume_without_appending(tmp_path, scoring_project):
    config, path, original, changed = scoring_project
    settings = make_settings(tmp_path, concurrency=1)
    first = run_eval(config, settings, max_cost_usd=1)
    assert first.incomplete and first.counts["total"] == 1
    assert first.records[0].scores["grade"]["score"] == 1
    results_file = first.path / "results.jsonl"
    before = results_file.read_bytes()
    fingerprint = config_fingerprint(config)

    path.write_text(changed, encoding="utf-8")
    assert config_fingerprint(config) != fingerprint
    with pytest.raises(StorageError, match="config does not match"):
        run_eval(config, settings, resume_run_id=first.run_id, max_cost_usd=3)
    assert results_file.read_bytes() == before

    path.write_text(original, encoding="utf-8")
    assert config_fingerprint(config) == fingerprint
    resumed = run_eval(config, settings, resume_run_id=first.run_id, max_cost_usd=3)
    assert not resumed.incomplete
    assert resumed.counts["total"] == 2
    assert [r.scores["grade"]["score"] for r in resumed.records] == [1, 1]


def test_missing_scoring_file_changes_fingerprint(scoring_project):
    config, path, _, _ = scoring_project
    before = config_fingerprint(config)
    path.unlink()
    assert config_fingerprint(config) != before


def test_source_backed_fingerprint_also_includes_scoring_files(tmp_path, scoring_project):
    config, path, _, changed = scoring_project
    (tmp_path / "source.py").write_text("# source code\n", encoding="utf-8")
    from evaling.config.schema import CaseSourceRef

    config.cases = CaseSourceRef(source="source.py:Source")
    before = config_fingerprint(config)
    path.write_text(changed, encoding="utf-8")
    assert config_fingerprint(config) != before
