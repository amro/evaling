import json

import pytest

from evaling.config import EvalConfig, Settings
from evaling.engine import run_eval
from evaling.storage import RunStore


def make_config(tmp_path, models=None, cases=None, variants=None):
    cfg = EvalConfig.model_validate(
        {
            "models": models or [{"id": "m1", "provider": "mock"}],
            "variants": variants
            or [{"name": "v1", "prompt": [{"role": "user", "content": "{{ q }}"}]}],
            "cases": cases
            or [{"id": "c1", "vars": {"q": "alpha"}}, {"id": "c2", "vars": {"q": "beta"}}],
            "scorecard": [{"criterion": "acc", "scorer": {"type": "exact"}}],
        }
    )
    cfg._base_dir = tmp_path
    return cfg


def make_settings(tmp_path, cache=False, concurrency=4):
    return Settings.model_validate(
        {
            "output_dir": str(tmp_path / "runs"),
            "cache_dir": str(tmp_path / "cache"),
            "cache": cache,
            "concurrency": concurrency,
        }
    )


def test_runs_full_matrix(tmp_path):
    config = make_config(
        tmp_path,
        models=[{"id": "m1", "provider": "mock"}, {"id": "m2", "provider": "mock"}],
        variants=[
            {"name": "v1", "prompt": [{"role": "user", "content": "{{ q }}"}]},
            {"name": "v2", "prompt": [{"role": "user", "content": "Q: {{ q }}"}]},
        ],
    )
    result = run_eval(config, make_settings(tmp_path))
    assert result.counts == {"total": 8, "succeeded": 8, "failed": 0, "cached": 0}
    keys = {r.key for r in result.records}
    assert ("v2", "m2", "c2") in keys
    by_key = {r.key: r for r in result.records}
    assert by_key[("v1", "m1", "c1")].output == "alpha"
    assert by_key[("v2", "m1", "c2")].output == "Q: beta"


def test_results_persisted_as_jsonl(tmp_path):
    config = make_config(tmp_path)
    result = run_eval(config, make_settings(tmp_path))
    lines = (result.path / "results.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert all("output" in json.loads(line) for line in lines)

    meta = json.loads((result.path / "run.json").read_text())
    assert meta["status"] == "complete"
    assert meta["counts"]["total"] == 2
    assert meta["totals"]["cost_usd"] == 0.0


def test_failing_model_does_not_abort_run(tmp_path):
    config = make_config(
        tmp_path,
        models=[
            {"id": "good", "provider": "mock"},
            {"id": "bad", "provider": "mock", "params": {"error": "fatal"}},
        ],
    )
    result = run_eval(config, make_settings(tmp_path))
    assert result.counts["total"] == 4
    assert result.counts["succeeded"] == 2
    assert result.counts["failed"] == 2
    failed = [r for r in result.records if r.error]
    assert all(r.model == "bad" for r in failed)
    assert all("mock fatal error" in r.error for r in failed)


def test_transient_failures_retried(tmp_path):
    config = make_config(
        tmp_path,
        models=[{"id": "flaky", "provider": "mock", "params": {"fail_times": 2}}],
        cases=[{"id": "c1", "vars": {"q": "x"}}],
    )
    result = run_eval(config, make_settings(tmp_path))
    assert result.counts["failed"] == 0
    assert result.records[0].output == "x"


def test_template_error_recorded_per_cell(tmp_path):
    config = make_config(
        tmp_path,
        variants=[{"name": "v1", "prompt": [{"role": "user", "content": "{{ missing }}"}]}],
    )
    result = run_eval(config, make_settings(tmp_path))
    assert result.counts["failed"] == 2
    assert "'missing' is undefined" in result.records[0].error


def test_cache_hit_on_second_run(tmp_path):
    config = make_config(tmp_path)
    settings = make_settings(tmp_path, cache=True)

    first = run_eval(config, settings)
    assert first.counts["cached"] == 0

    second = run_eval(config, settings)
    assert second.counts["cached"] == 2
    assert {r.output for r in second.records} == {r.output for r in first.records}
    assert all(r.latency_ms is None for r in second.records)


def test_cache_disabled_never_caches(tmp_path):
    config = make_config(tmp_path)
    settings = make_settings(tmp_path, cache=False)
    run_eval(config, settings)
    second = run_eval(config, settings)
    assert second.counts["cached"] == 0
    assert not (tmp_path / "cache").exists()


def test_resume_executes_only_missing_cells(tmp_path):
    config = make_config(tmp_path)
    settings = make_settings(tmp_path)

    interrupted = run_eval(config, settings)
    results_path = interrupted.path / "results.jsonl"
    lines = results_path.read_text().splitlines()
    kept, removed = lines[0], json.loads(lines[1])
    results_path.write_text(kept + "\n")

    resumed = run_eval(config, settings, resume_run_id=interrupted.run_id)
    assert resumed.run_id == interrupted.run_id
    assert resumed.counts["total"] == 2
    final = RunStore(settings.output_dir).load_results(interrupted.run_id)
    assert len(final) == 2
    assert {r.case_id for r in final} == {"c1", "c2"}
    assert removed["case_id"] in {r.case_id for r in final}


def test_media_artifacts_stored(tmp_path):
    (tmp_path / "dog.png").write_bytes(b"png-bytes")
    config = make_config(
        tmp_path,
        variants=[
            {
                "name": "v1",
                "prompt": [
                    {
                        "role": "user",
                        "content": [{"text": "{{ q }}"}, {"image": "dog.png"}],
                    }
                ],
            }
        ],
        cases=[{"id": "c1", "vars": {"q": "look"}}],
    )
    result = run_eval(config, make_settings(tmp_path))
    artifacts = list((result.path / "artifacts").iterdir())
    assert len(artifacts) == 1
    assert artifacts[0].read_bytes() == b"png-bytes"
    assert result.records[0].output.startswith("look [image:")


def test_run_result_totals_sum_tokens(tmp_path):
    config = make_config(tmp_path)
    result = run_eval(config, make_settings(tmp_path))
    assert result.totals["input_tokens"] == sum(r.input_tokens for r in result.records)
    assert result.totals["output_tokens"] > 0


def test_resume_missing_run_raises(tmp_path):
    from evaling.storage import StorageError

    config = make_config(tmp_path)
    with pytest.raises(StorageError, match="run not found"):
        run_eval(config, make_settings(tmp_path), resume_run_id="ghost")
