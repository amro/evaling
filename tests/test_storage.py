import json

import pytest
import yaml

from evaling.config import Case, EvalConfig, Message
from evaling.content import resolve_media
from evaling.render import render_messages
from evaling.storage import ResultRecord, RunStore, StorageError, serialize_messages


@pytest.fixture
def config():
    return EvalConfig.model_validate(
        {
            "models": [{"id": "m1", "provider": "mock"}],
            "variants": [{"name": "v1", "prompt": "p.yaml"}],
            "cases": [{"vars": {"q": "hi"}}],
            "scorecard": [{"criterion": "acc", "scorer": {"type": "exact"}}],
        }
    )


def record(variant="v1", model="m1", case_id="case-1", **kwargs):
    return ResultRecord(variant=variant, model=model, case_id=case_id, **kwargs)


def test_create_run_writes_meta_and_snapshot(tmp_path, config):
    writer = RunStore(tmp_path).create_run(config, label="baseline-candidate")
    assert (writer.path / "artifacts").is_dir()

    meta = json.loads((writer.path / "run.json").read_text())
    assert meta["id"] == writer.run_id
    assert meta["label"] == "baseline-candidate"
    assert meta["status"] == "running"
    assert meta["counts"] is None

    snapshot = yaml.safe_load((writer.path / "config.snapshot.yaml").read_text())
    assert snapshot["models"][0]["id"] == "m1"


def test_run_ids_are_unique_and_sortable(tmp_path, config):
    store = RunStore(tmp_path)
    ids = [store.create_run(config).run_id for _ in range(5)]
    assert len(set(ids)) == 5
    assert [run["id"] for run in store.list_runs()] == sorted(ids)


def test_append_and_load_results_roundtrip(tmp_path, config):
    store = RunStore(tmp_path)
    writer = store.create_run(config)
    writer.append_result(record(output="hello", input_tokens=3, latency_ms=1.5))
    writer.append_result(record(case_id="case-2", error="boom"))

    records = store.load_results(writer.run_id)
    assert len(records) == 2
    assert records[0].output == "hello"
    assert records[0].scores == {}
    assert records[1].error == "boom"
    assert records[1].key == ("v1", "m1", "case-2")


def test_completed_keys_reflect_appended_records(tmp_path, config):
    writer = RunStore(tmp_path).create_run(config)
    assert writer.completed_keys() == set()
    writer.append_result(record())
    writer.append_result(record(case_id="case-2"))
    assert writer.completed_keys() == {("v1", "m1", "case-1"), ("v1", "m1", "case-2")}


def test_finalize_rewrites_meta(tmp_path, config):
    store = RunStore(tmp_path)
    writer = store.create_run(config)
    writer.finalize({"total": 2, "failed": 0}, {"cost_usd": 0.0})

    meta = store.load_meta(writer.run_id)
    assert meta["status"] == "complete"
    assert meta["finished_at"] is not None
    assert meta["counts"] == {"total": 2, "failed": 0}


def test_store_artifact_content_addressed_and_idempotent(tmp_path, config):
    (tmp_path / "a.png").write_bytes(b"same-bytes")
    (tmp_path / "b.png").write_bytes(b"same-bytes")
    ref_a = resolve_media("image", "a.png", tmp_path)
    ref_b = resolve_media("image", "b.png", tmp_path)

    writer = RunStore(tmp_path / "runs").create_run(config)
    rel_a = writer.store_artifact(ref_a)
    rel_b = writer.store_artifact(ref_b)
    assert rel_a == rel_b  # identical content, one artifact
    artifacts = list((writer.path / "artifacts").iterdir())
    assert len(artifacts) == 1
    assert artifacts[0].read_bytes() == b"same-bytes"
    assert artifacts[0].name == f"{ref_a.sha256}.png"


def test_serialize_messages_with_and_without_source(tmp_path):
    (tmp_path / "dog.png").write_bytes(b"png")
    messages = [
        Message.model_validate(
            {"role": "user", "content": [{"text": "{{ q }}"}, {"image": "dog.png"}]}
        )
    ]
    rendered = render_messages(messages, Case(vars={"q": "breed?"}), tmp_path)

    with_source = serialize_messages(rendered)
    assert with_source[0]["parts"][0] == {"type": "text", "text": "breed?"}
    assert with_source[0]["parts"][1]["type"] == "image"
    assert "source" in with_source[0]["parts"][1]

    without = serialize_messages(rendered, include_source=False)
    assert "source" not in without[0]["parts"][1]
    assert without[0]["parts"][1]["sha256"] == with_source[0]["parts"][1]["sha256"]


def test_torn_final_line_tolerated(tmp_path, config):
    # Regression: a process killed mid-append leaves a truncated last line;
    # loading and resuming must treat it as end-of-file, not crash.
    store = RunStore(tmp_path)
    writer = store.create_run(config)
    writer.append_result(record())
    with writer.results_path.open("a") as handle:
        handle.write('{"variant": "v1", "model": "m1", "case_')  # torn mid-write

    assert len(store.load_results(writer.run_id)) == 1
    reopened = store.open_run(writer.run_id)
    assert reopened.completed_keys() == {("v1", "m1", "case-1")}


def test_open_run_truncates_torn_tail_before_appending(tmp_path, config):
    store = RunStore(tmp_path)
    writer = store.create_run(config)
    writer.append_result(record())
    with writer.results_path.open("a") as handle:
        handle.write('{"torn')

    reopened = store.open_run(writer.run_id)
    reopened.append_result(record(case_id="case-2"))
    # every line must now parse: the torn tail was removed, not buried mid-file
    records = store.load_results(writer.run_id)
    assert [r.case_id for r in records] == ["case-1", "case-2"]


def test_corrupt_middle_line_raises(tmp_path, config):
    store = RunStore(tmp_path)
    writer = store.create_run(config)
    writer.append_result(record())
    with writer.results_path.open("a") as handle:
        handle.write("{corrupt}\n")
    writer.append_result(record(case_id="case-2"))

    with pytest.raises(StorageError, match="corrupt record at line 2"):
        store.load_results(writer.run_id)


def test_open_run_missing_raises(tmp_path):
    with pytest.raises(StorageError, match="run not found"):
        RunStore(tmp_path).open_run("nope")


def test_load_results_empty_when_no_records(tmp_path, config):
    store = RunStore(tmp_path)
    writer = store.create_run(config)
    assert store.load_results(writer.run_id) == []


def test_list_runs_empty_output_dir(tmp_path):
    assert RunStore(tmp_path / "missing").list_runs() == []
