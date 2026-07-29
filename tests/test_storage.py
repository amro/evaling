import json
import tracemalloc
from pathlib import Path

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

    meta = json.loads((writer.path / "run.json").read_text(encoding="utf-8"))
    assert meta["id"] == writer.run_id
    assert meta["label"] == "baseline-candidate"
    assert meta["status"] == "running"
    assert meta["counts"] is None

    snapshot = yaml.safe_load((writer.path / "config.snapshot.yaml").read_text(encoding="utf-8"))
    assert snapshot["models"][0]["id"] == "m1"


def test_runs_list_in_creation_order(tmp_path, config):
    # Regression: back-to-back runs usually share a millisecond timestamp, so
    # lexicographic id order left "latest" to the random suffix. Creation
    # order must come from created_ns, not the id string.
    store = RunStore(tmp_path)
    ids = [store.create_run(config).run_id for _ in range(10)]
    assert len(set(ids)) == 10
    assert [run["id"] for run in store.list_runs()] == ids


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


def test_iter_results_streams_instead_of_slurping(tmp_path, config):
    # Regression: iter_results read every line into a list before yielding,
    # so the large runs it exists for were held in memory whole anyway.
    store = RunStore(tmp_path)
    writer = store.create_run(config)
    fat = "x" * 8000
    with writer.results_path.open("a", encoding="utf-8") as handle:
        for i in range(1500):
            line = json.dumps({"variant": "v1", "model": "m1", "case_id": f"c{i}", "output": fat})
            handle.write(line + "\n")
    file_size = writer.results_path.stat().st_size  # ~12 MB

    tracemalloc.start()
    iterator = store.iter_results(writer.run_id)
    first = next(iterator)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    iterator.close()

    assert first.case_id == "c0"
    assert peak < file_size / 4  # a few lines in flight, never the whole file


def test_store_artifact_crash_mid_copy_leaves_no_partial(tmp_path, config, monkeypatch):
    # Regression: a copy that died half-way left a partial file at the final
    # name, and the existence check then trusted it forever.
    (tmp_path / "a.png").write_bytes(b"the-full-content")
    ref = resolve_media("image", "a.png", tmp_path)
    writer = RunStore(tmp_path / "runs").create_run(config)

    def dies_mid_copy(src, dst, **kwargs):
        Path(dst).write_bytes(b"the-fu")
        raise OSError("disk full")

    monkeypatch.setattr("evaling.storage.shutil.copyfile", dies_mid_copy)
    with pytest.raises(OSError, match="disk full"):
        writer.store_artifact(ref)
    monkeypatch.undo()

    # nothing half-written survives the crash, and a retry stores the real bytes
    assert list((writer.path / "artifacts").iterdir()) == []
    rel = writer.store_artifact(ref)
    assert (writer.path / rel).read_bytes() == b"the-full-content"


def test_torn_final_line_tolerated(tmp_path, config):
    # Regression: a process killed mid-append leaves a truncated last line;
    # loading and resuming must treat it as end-of-file, not crash.
    store = RunStore(tmp_path)
    writer = store.create_run(config)
    writer.append_result(record())
    with writer.results_path.open("a", encoding="utf-8") as handle:
        handle.write('{"variant": "v1", "model": "m1", "case_')  # torn mid-write

    assert len(store.load_results(writer.run_id)) == 1
    reopened = store.open_run(writer.run_id)
    assert reopened.completed_keys() == {("v1", "m1", "case-1")}


def test_open_run_truncates_torn_tail_before_appending(tmp_path, config):
    store = RunStore(tmp_path)
    writer = store.create_run(config)
    writer.append_result(record())
    with writer.results_path.open("a", encoding="utf-8") as handle:
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
    with writer.results_path.open("a", encoding="utf-8") as handle:
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


class TestRunRefs:
    def test_latest_resolves_newest(self, tmp_path, config):
        store = RunStore(tmp_path)
        ids = [store.create_run(config).run_id for _ in range(3)]
        assert store.resolve_ref("latest") == ids[-1]

    def test_latest_with_no_runs_raises(self, tmp_path):
        with pytest.raises(StorageError, match="no runs found"):
            RunStore(tmp_path).resolve_ref("latest")

    def test_run_id_passes_through(self, tmp_path, config):
        store = RunStore(tmp_path)
        run_id = store.create_run(config).run_id
        assert store.resolve_ref(run_id) == run_id

    def test_label_resolves_most_recent_match(self, tmp_path, config):
        store = RunStore(tmp_path)
        store.create_run(config, label="candidate")
        second = store.create_run(config, label="candidate")
        assert store.resolve_ref("candidate") == second.run_id

    def test_baseline_set_get_resolve(self, tmp_path, config):
        store = RunStore(tmp_path)
        run_id = store.create_run(config).run_id
        assert store.get_baseline() is None
        store.set_baseline(run_id)
        assert store.get_baseline() == run_id
        assert store.resolve_ref("baseline") == run_id

    def test_baseline_unpinned_raises(self, tmp_path, config):
        store = RunStore(tmp_path)
        store.create_run(config)
        with pytest.raises(StorageError, match="no baseline pinned"):
            store.resolve_ref("baseline")

    def test_set_baseline_requires_existing_run(self, tmp_path):
        with pytest.raises(StorageError, match="run not found"):
            RunStore(tmp_path).set_baseline("ghost")

    def test_unknown_ref_raises(self, tmp_path, config):
        store = RunStore(tmp_path)
        store.create_run(config)
        with pytest.raises(StorageError, match="no run matches 'nope'"):
            store.resolve_ref("nope")

    def test_baseline_file_ignored_by_list_runs(self, tmp_path, config):
        store = RunStore(tmp_path)
        run_id = store.create_run(config).run_id
        store.set_baseline(run_id)
        assert [run["id"] for run in store.list_runs()] == [run_id]
