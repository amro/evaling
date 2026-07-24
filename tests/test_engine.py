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


def test_arbitrary_template_runtime_error_isolated_per_cell(tmp_path):
    # Regression: a ZeroDivisionError inside a Jinja2 expression must not kill
    # the run, lose cells, or leave run.json stuck at "running".
    config = make_config(
        tmp_path,
        variants=[
            {"name": "good", "prompt": [{"role": "user", "content": "{{ q }}"}]},
            {"name": "bad", "prompt": [{"role": "user", "content": "{{ 1 / q }}"}]},
        ],
        cases=[{"id": "c1", "vars": {"q": 0}}, {"id": "c2", "vars": {"q": 2}}],
    )
    result = run_eval(config, make_settings(tmp_path))
    assert result.counts["total"] == 4
    assert result.counts["failed"] == 1
    by_key = {r.key: r for r in result.records}
    assert "ZeroDivisionError" in by_key[("bad", "m1", "c1")].error
    assert by_key[("bad", "m1", "c2")].output == "0.5"

    meta = json.loads((result.path / "run.json").read_text())
    assert meta["status"] == "complete"


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


def test_identical_concurrent_requests_single_flight_through_cache(tmp_path):
    # Regression: two cells with byte-identical requests used to both miss the
    # cache and both call the provider. The per-key lock makes the second
    # waiter hit the first's cached response.
    config = make_config(
        tmp_path,
        cases=[
            {"id": "c1", "vars": {"q": "same"}},
            {"id": "c2", "vars": {"q": "same"}},
            {"id": "c3", "vars": {"q": "same"}},
        ],
    )
    result = run_eval(config, make_settings(tmp_path, cache=True, concurrency=3))
    assert result.counts["cached"] == 2
    assert sum(1 for r in result.records if not r.cached) == 1


def test_cache_disabled_never_caches(tmp_path):
    config = make_config(tmp_path)
    settings = make_settings(tmp_path, cache=False)
    run_eval(config, settings)
    second = run_eval(config, settings)
    assert second.counts["cached"] == 0
    assert not (tmp_path / "cache").exists()


def simulate_interruption(run_path, keep_lines=1):
    """Rewind a finished run to look like a process crash: partial results, status running."""
    results_path = run_path / "results.jsonl"
    lines = results_path.read_text().splitlines()
    kept, removed = lines[:keep_lines], [json.loads(line) for line in lines[keep_lines:]]
    results_path.write_text("".join(line + "\n" for line in kept))
    meta_path = run_path / "run.json"
    meta = json.loads(meta_path.read_text())
    meta.update(status="running", finished_at=None, counts=None, totals=None)
    meta_path.write_text(json.dumps(meta))
    return removed


def test_resume_executes_only_missing_cells(tmp_path):
    config = make_config(tmp_path)
    settings = make_settings(tmp_path)

    interrupted = run_eval(config, settings)
    [removed] = simulate_interruption(interrupted.path)

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


def test_records_carry_scores_and_run_json_aggregates(tmp_path):
    # 'plain' echoes the question so exact-vs-expected passes when expected==question
    config = make_config(
        tmp_path,
        cases=[
            {"id": "c1", "vars": {"q": "alpha"}, "expected": "alpha"},
            {"id": "c2", "vars": {"q": "beta"}, "expected": "GAMMA"},
        ],
    )
    result = run_eval(config, make_settings(tmp_path))
    by_case = {r.case_id: r for r in result.records}
    assert by_case["c1"].scores["acc"]["passed"] is True
    assert by_case["c2"].scores["acc"]["passed"] is False
    assert by_case["c2"].scores["acc"]["detail"] == "expected 'GAMMA'"

    assert result.aggregates["overall"] == {
        "cases": 2,
        "score": 0.5,
        "pass_rate": 0.5,
        "errors": 0,
    }
    meta = json.loads((result.path / "run.json").read_text())
    assert meta["aggregates"]["overall"]["pass_rate"] == 0.5


def test_gate_from_thresholds(tmp_path):
    config = make_config(
        tmp_path,
        cases=[{"id": "c1", "vars": {"q": "alpha"}, "expected": "alpha"}],
    )
    config.thresholds.min_pass_rate = 0.9
    result = run_eval(config, make_settings(tmp_path))
    assert result.gate.passed

    config.thresholds.min_pass_rate = None
    config.thresholds.min_score = 2.0  # unreachable
    failed = run_eval(config, make_settings(tmp_path))
    assert not failed.gate.passed
    meta = json.loads((failed.path / "run.json").read_text())
    assert meta["gate"]["passed"] is False


def test_no_thresholds_no_gate(tmp_path):
    result = run_eval(make_config(tmp_path), make_settings(tmp_path))
    assert result.gate is None


def test_baseline_run_id_gates_regression(tmp_path):
    settings = make_settings(tmp_path)
    good = make_config(tmp_path, cases=[{"id": "c1", "vars": {"q": "alpha"}, "expected": "alpha"}])
    baseline = run_eval(good, settings)

    worse = make_config(tmp_path, cases=[{"id": "c1", "vars": {"q": "alpha"}, "expected": "WRONG"}])
    result = run_eval(worse, settings, baseline_run_id=baseline.run_id)
    assert not result.gate.passed
    assert result.gate.checks[0]["name"] == "baseline"

    same = run_eval(good, settings, baseline_run_id=baseline.run_id)
    assert same.gate.passed


def test_scorer_crash_fails_criterion_not_run(tmp_path):
    config = make_config(
        tmp_path,
        cases=[{"id": "c1", "vars": {"q": "alpha"}}],  # no expected -> exact scorer raises
    )
    result = run_eval(config, make_settings(tmp_path))
    assert result.counts["failed"] == 0  # the model call itself succeeded
    entry = result.records[0].scores["acc"]
    assert entry["passed"] is False
    assert "expected" in entry["error"]
    assert result.aggregates["overall"]["pass_rate"] == 0.0


def test_resume_missing_run_raises(tmp_path):
    from evaling.storage import StorageError

    config = make_config(tmp_path)
    with pytest.raises(StorageError, match="run not found"):
        run_eval(config, make_settings(tmp_path), resume_run_id="ghost")


def test_resume_with_mismatched_config_rejected(tmp_path):
    # Regression: resuming with a different config used to silently mix two
    # configs' results into one run directory.
    from evaling.storage import StorageError

    settings = make_settings(tmp_path)
    interrupted = run_eval(make_config(tmp_path), settings)
    simulate_interruption(interrupted.path)

    other = make_config(tmp_path, models=[{"id": "other-model", "provider": "mock"}])
    with pytest.raises(StorageError, match="config does not match"):
        run_eval(other, settings, resume_run_id=interrupted.run_id)


def test_resume_of_complete_run_rejected(tmp_path):
    from evaling.storage import StorageError

    config = make_config(tmp_path)
    settings = make_settings(tmp_path)
    finished = run_eval(config, settings)
    with pytest.raises(StorageError, match="already complete"):
        run_eval(config, settings, resume_run_id=finished.run_id)
