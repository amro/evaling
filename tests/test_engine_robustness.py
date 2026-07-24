"""Regressions for bugs found in the full-codebase review.

Each test here failed before its fix; they exist so the same class of problem
can't come back quietly.
"""

import asyncio
import json
from pathlib import Path

import pytest

from evaling.cache import ResponseCache
from evaling.config import Case, Message, ModelSpec
from evaling.engine import run_eval
from evaling.providers import _REGISTRY
from evaling.providers.base import Completion
from evaling.providers.mock import MockProvider
from evaling.render import render_messages
from evaling.storage import RunStore, record_from_dict, write_json_atomic
from helpers import make_config, make_settings


class ConcurrencyProbe(MockProvider):
    """Records peak in-flight calls, and can report an unknown cost."""

    peak = 0
    now = 0
    cost: float | None = 0.0

    async def complete(self, request):
        type(self).now += 1
        type(self).peak = max(type(self).peak, type(self).now)
        try:
            await asyncio.sleep(0.02)
            return Completion(text="ok", input_tokens=5, output_tokens=5, cost_usd=self.cost)
        finally:
            type(self).now -= 1

    @classmethod
    def reset(cls, cost):
        cls.peak = cls.now = 0
        cls.cost = cost


@pytest.fixture
def probe(monkeypatch):
    monkeypatch.setitem(_REGISTRY, "mock", ConcurrencyProbe)
    return ConcurrencyProbe


def eight_cases(tmp_path):
    return make_config(tmp_path, cases=[{"id": f"c{i}", "vars": {"q": str(i)}} for i in range(8)])


class TestCostBudgetConcurrency:
    """--max-cost used to silently serialize runs against unpriced models."""

    def test_unpriced_models_still_run_concurrently(self, tmp_path, probe):
        probe.reset(cost=None)  # a local model: no pricing, no cost reported
        result = run_eval(
            eight_cases(tmp_path), make_settings(tmp_path, concurrency=8), max_cost_usd=100.0
        )
        assert probe.peak > 1, "an unenforceable cost cap must not serialize the run"
        assert result.counts["succeeded"] == 8

    def test_unenforceable_cap_warns(self, tmp_path, probe):
        probe.reset(cost=None)
        result = run_eval(
            eight_cases(tmp_path), make_settings(tmp_path, concurrency=4), max_cost_usd=100.0
        )
        assert any("could not be enforced" in w for w in result.warnings)
        # and it's persisted with the run, not just printed
        meta = json.loads((result.path / "run.json").read_text(encoding="utf-8"))
        assert meta["warnings"]

    def test_priced_models_are_still_capped(self, tmp_path, probe):
        probe.reset(cost=1.0)
        result = run_eval(
            eight_cases(tmp_path), make_settings(tmp_path, concurrency=1), max_cost_usd=3.0
        )
        assert result.totals["cost_usd"] <= 3.0
        assert result.counts["failed"] > 0  # remaining cells skipped by the budget
        assert not result.warnings

    def test_no_cap_means_no_warning(self, tmp_path, probe):
        probe.reset(cost=None)
        result = run_eval(eight_cases(tmp_path), make_settings(tmp_path, concurrency=4))
        assert result.warnings == []
        assert probe.peak > 1


class TestCacheCompatibility:
    def rendered(self, tmp_path):
        return render_messages([Message(role="user", content="hi")], Case(), tmp_path)

    def test_unknown_entry_field_is_a_miss_not_a_crash(self, tmp_path):
        cache = ResponseCache(tmp_path / "cache")
        spec = ModelSpec.model_validate({"id": "m", "provider": "mock"})
        key = cache.key_for(spec, self.rendered(tmp_path))
        cache.put(key, Completion(text="hi"))

        path = cache._path(key)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["field_from_a_newer_evaling"] = 1
        path.write_text(json.dumps(data), encoding="utf-8")

        assert cache.get(key).text == "hi"  # tolerated, not TypeError

    def test_garbage_entry_is_a_miss(self, tmp_path):
        cache = ResponseCache(tmp_path / "cache")
        spec = ModelSpec.model_validate({"id": "m", "provider": "mock"})
        key = cache.key_for(spec, self.rendered(tmp_path))
        cache.put(key, Completion(text="hi"))
        cache._path(key).write_text('"just a string"', encoding="utf-8")
        assert cache.get(key) is None

    @pytest.mark.parametrize(
        "change",
        [
            {"timeout_s": 30},
            {"max_retries": 5},
            {"api_key_env": "OTHER_KEY"},
            {"params": {"pricing": {"input": 1, "output": 2}}},
        ],
    )
    def test_operational_knobs_do_not_invalidate_the_cache(self, tmp_path, change):
        # Bumping a timeout or correcting a price must not discard every
        # cached response.
        cache = ResponseCache(tmp_path / "cache")
        base = ModelSpec.model_validate({"id": "m", "provider": "mock"})
        changed = ModelSpec.model_validate({"id": "m", "provider": "mock", **change})
        messages = self.rendered(tmp_path)
        assert cache.key_for(base, messages) == cache.key_for(changed, messages)

    @pytest.mark.parametrize(
        "change",
        [
            {"id": "other-model"},
            {"provider": "openai-compatible", "base_url": "http://localhost:1/v1"},
            {"params": {"temperature": 0.9}},
            {"params": {"model": "gpt-5.2"}},
        ],
    )
    def test_response_affecting_changes_do_invalidate(self, tmp_path, change):
        cache = ResponseCache(tmp_path / "cache")
        base = ModelSpec.model_validate({"id": "m", "provider": "mock"})
        changed = ModelSpec.model_validate({"id": "m", "provider": "mock", **change})
        messages = self.rendered(tmp_path)
        assert cache.key_for(base, messages) != cache.key_for(changed, messages)

    def test_put_failure_does_not_raise(self, tmp_path, monkeypatch):
        # An unwritable cache must not cost the caller a paid-for response.
        # The failure is injected rather than staged with chmod: directory
        # permissions don't deny writes on Windows, and the behaviour under
        # test is our handling of the error, not the OS that raises it.
        def explode(*args, **kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(Path, "write_text", explode)
        cache = ResponseCache(tmp_path / "cache")
        spec = ModelSpec.model_validate({"id": "m", "provider": "mock"})
        key = cache.key_for(spec, self.rendered(tmp_path))
        cache.put(key, Completion(text="hi"))  # must not raise
        monkeypatch.undo()
        assert cache.get(key) is None


class TestStorageDurability:
    def test_corrupt_run_json_does_not_break_listing(self, tmp_path):
        settings = make_settings(tmp_path)
        good = run_eval(make_config(tmp_path), settings)
        broken = run_eval(make_config(tmp_path), settings)
        (broken.path / "run.json").write_text("{half-written", encoding="utf-8")

        store = RunStore(settings.output_dir)
        listed = [meta["id"] for meta in store.list_runs()]
        assert listed == [good.run_id]  # the healthy run is still visible

    def test_corrupt_run_json_names_the_run_when_opened(self, tmp_path):
        from evaling.storage import StorageError

        settings = make_settings(tmp_path)
        result = run_eval(make_config(tmp_path), settings)
        (result.path / "run.json").write_text("{half-written", encoding="utf-8")
        with pytest.raises(StorageError, match="run metadata is corrupt"):
            RunStore(settings.output_dir).load_meta(result.run_id)

    def test_reads_never_mutate_the_run(self, tmp_path):
        settings = make_settings(tmp_path)
        result = run_eval(make_config(tmp_path), settings)
        results_path = result.path / "results.jsonl"
        with results_path.open("a", encoding="utf-8") as handle:
            handle.write('{"torn')  # a crash artifact
        before = results_path.read_text(encoding="utf-8")

        store = RunStore(settings.output_dir)
        store.load_meta(result.run_id)
        store.load_results(result.run_id)
        assert results_path.read_text(encoding="utf-8") == before, "a read must not rewrite the run"

    def test_resume_still_repairs(self, tmp_path):
        settings = make_settings(tmp_path)
        result = run_eval(make_config(tmp_path), settings)
        results_path = result.path / "results.jsonl"
        with results_path.open("a", encoding="utf-8") as handle:
            handle.write('{"torn')

        RunStore(settings.output_dir).open_run(result.run_id)  # write-open
        assert '{"torn' not in results_path.read_text(encoding="utf-8")

    def test_run_json_is_written_atomically(self, tmp_path):
        target = tmp_path / "run.json"
        write_json_atomic(target, {"a": 1})
        assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
        assert list(tmp_path.glob(".*tmp*")) == []  # no leftovers

    def test_records_tolerate_unknown_fields(self):
        record = record_from_dict(
            {
                "variant": "v",
                "model": "m",
                "case_id": "c",
                "output": "hi",
                "field_from_a_newer_evaling": {"nested": True},
            }
        )
        assert record.output == "hi"

    def test_runs_record_a_format_version(self, tmp_path):
        settings = make_settings(tmp_path)
        result = run_eval(make_config(tmp_path), settings)
        meta = json.loads((result.path / "run.json").read_text(encoding="utf-8"))
        assert meta["format_version"] >= 1
