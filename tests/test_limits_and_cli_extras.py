"""Per-model limits, the cache command, validate, and init --provider."""

import asyncio
import time

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from evaling.cache import ResponseCache
from evaling.cli import main
from evaling.config import ModelSpec
from evaling.engine import run_eval
from evaling.limits import ModelLimiter, limiter_for
from evaling.providers import _REGISTRY
from evaling.providers.base import Completion
from evaling.providers.mock import MockProvider
from evaling.storage import RunStore
from helpers import make_config, make_settings

ENV = {"EVALING_USER_CONFIG": "/nonexistent", "EVALING_SECRETS": ""}


class Tracker(MockProvider):
    peak = 0
    now = 0
    starts: list[float] = []

    async def complete(self, request):
        type(self).now += 1
        type(self).peak = max(type(self).peak, type(self).now)
        type(self).starts.append(time.monotonic())
        try:
            await asyncio.sleep(0.02)
            return Completion(text="ok", cost_usd=0.0)
        finally:
            type(self).now -= 1

    @classmethod
    def reset(cls):
        cls.peak = cls.now = 0
        cls.starts = []


class TestModelLimiter:
    def test_unlimited_by_default(self):
        spec = ModelSpec.model_validate({"id": "m", "provider": "mock"})
        assert limiter_for(spec).unlimited

    def test_concurrency_cap(self):
        state = {"peak": 0, "now": 0}

        async def go():
            limiter = ModelLimiter(max_concurrency=2)

            async def worker():
                async with limiter:
                    state["now"] += 1
                    state["peak"] = max(state["peak"], state["now"])
                    await asyncio.sleep(0.01)
                    state["now"] -= 1

            await asyncio.gather(*(worker() for _ in range(6)))

        asyncio.run(go())
        assert state["peak"] == 2

    def test_rate_limit_waits_for_the_window(self):
        # A virtual clock keeps the test instant: sleeping advances time.
        clock = {"t": 1000.0}
        slept: list[float] = []

        async def fake_sleep(delay):
            slept.append(delay)
            clock["t"] += delay

        async def go():
            limiter = ModelLimiter(requests_per_minute=2, now=lambda: clock["t"], sleep=fake_sleep)
            for _ in range(4):
                async with limiter:
                    pass

        asyncio.run(go())
        # The real invariant: 4 requests at 2/min cannot fit in one window, so
        # the clock must advance at least a full minute. (A single wait frees
        # both slots when they were taken at the same instant — counting sleeps
        # would assert the implementation, not the limit.)
        assert clock["t"] - 1000.0 >= 60.0
        assert all(delay > 0 for delay in slept)

    def test_rate_limit_lets_everything_through_under_the_cap(self):
        slept: list[float] = []

        async def fake_sleep(delay):  # pragma: no cover - must never run
            slept.append(delay)

        async def go():
            limiter = ModelLimiter(requests_per_minute=10, sleep=fake_sleep)
            for _ in range(5):
                async with limiter:
                    pass

        asyncio.run(go())
        assert slept == []

    def test_semaphore_is_released_if_the_rate_wait_fails(self):
        # Otherwise one failure would permanently consume a concurrency slot.
        async def boom(delay):
            raise RuntimeError("clock exploded")

        clock = {"t": 0.0}

        async def go():
            limiter = ModelLimiter(
                max_concurrency=1, requests_per_minute=1, now=lambda: clock["t"], sleep=boom
            )
            async with limiter:  # consumes the single rpm slot
                pass
            with pytest.raises(RuntimeError):
                async with limiter:  # must wait, then fail in sleep
                    pass
            # the slot came back despite the failure
            assert limiter._semaphore._value == 1

        asyncio.run(go())


class TestPerModelConcurrency:
    def test_model_cap_overrides_global(self, tmp_path, monkeypatch):
        monkeypatch.setitem(_REGISTRY, "mock", Tracker)
        Tracker.reset()
        config = make_config(
            tmp_path,
            models=[{"id": "m1", "provider": "mock", "max_concurrency": 2}],
            cases=[{"id": f"c{i}", "vars": {"q": str(i)}} for i in range(8)],
        )
        run_eval(config, make_settings(tmp_path, concurrency=8))
        assert Tracker.peak <= 2

    def test_without_a_cap_the_global_setting_applies(self, tmp_path, monkeypatch):
        monkeypatch.setitem(_REGISTRY, "mock", Tracker)
        Tracker.reset()
        config = make_config(
            tmp_path, cases=[{"id": f"c{i}", "vars": {"q": str(i)}} for i in range(8)]
        )
        run_eval(config, make_settings(tmp_path, concurrency=4))
        assert 1 < Tracker.peak <= 4

    @pytest.mark.parametrize("bad", [{"max_concurrency": 0}, {"requests_per_minute": 0}])
    def test_schema_rejects_nonsense_limits(self, bad):
        with pytest.raises(ValidationError):
            ModelSpec.model_validate({"id": "m", "provider": "mock", **bad})


class TestCacheCommand:
    def cli(self, tmp_path, *args):
        base = ["-o", str(tmp_path / "runs"), "--cache-dir", str(tmp_path / "cache")]
        return CliRunner().invoke(main, base + list(args), env=ENV, catch_exceptions=False)

    def seed(self, tmp_path):
        config = make_config(tmp_path)
        settings = make_settings(tmp_path, cache=True)
        run_eval(config, settings)
        return settings

    def test_info_reports_entries(self, tmp_path):
        self.seed(tmp_path)
        result = self.cli(tmp_path, "cache", "info")
        assert result.exit_code == 0, result.output
        assert "entries" in result.output

    def test_info_json(self, tmp_path):
        self.seed(tmp_path)
        import json

        result = self.cli(tmp_path, "--json", "cache", "info")
        stats = json.loads(result.output)
        assert stats["entries"] >= 1 and stats["bytes"] > 0

    def test_clear_removes_entries(self, tmp_path):
        self.seed(tmp_path)
        result = self.cli(tmp_path, "cache", "clear", "--yes")
        assert result.exit_code == 0, result.output
        assert "removed" in result.output
        assert ResponseCache(tmp_path / "cache").stats()["entries"] == 0

    def test_clear_empty_cache_is_graceful(self, tmp_path):
        result = self.cli(tmp_path, "cache", "clear", "--yes")
        assert result.exit_code == 0
        assert "already empty" in result.output

    def test_older_than_keeps_fresh_entries(self, tmp_path):
        self.seed(tmp_path)
        cache = ResponseCache(tmp_path / "cache")
        before = cache.stats()["entries"]
        result = self.cli(tmp_path, "cache", "clear", "--older-than", "7", "--yes")
        assert result.exit_code == 0
        assert cache.stats()["entries"] == before  # nothing is a week old yet


class TestValidateCommand:
    def cli(self, tmp_path, *args):
        base = ["-o", str(tmp_path / "runs"), "--cache-dir", str(tmp_path / "cache")]
        return CliRunner().invoke(main, base + list(args), env=ENV, catch_exceptions=False)

    def config(self, tmp_path, content=None):
        path = tmp_path / "eval.yaml"
        path.write_text(
            content
            or (
                "models: [{id: mock, provider: mock}]\n"
                "variants:\n  - name: v1\n"
                '    prompt: [{role: user, content: "{{ q }}"}]\n'
                "cases: [{id: c1, vars: {q: alpha}, expected: alpha}]\n"
                "scorecard: [{criterion: acc, scorer: {type: exact}}]\n"
            ),
            encoding="utf-8",
        )
        return path

    def test_validate_passes_and_calls_nothing(self, tmp_path):
        result = self.cli(tmp_path, "validate", str(self.config(tmp_path)))
        assert result.exit_code == 0, result.output
        assert "requests would be made" in result.output
        assert not (tmp_path / "runs").exists()

    def test_validate_reports_errors(self, tmp_path):
        broken = (
            self.config(tmp_path).read_text(encoding="utf-8").replace("{{ q }}", "{{ missing }}")
        )
        result = self.cli(tmp_path, "validate", str(self.config(tmp_path, broken)))
        assert result.exit_code == 2
        assert "'missing' is undefined" in result.output


class TestInitProvider:
    def test_scaffold_includes_gitignore_and_secrets_example(self):
        runner = CliRunner()
        with runner.isolated_filesystem() as root:
            from pathlib import Path

            result = runner.invoke(main, ["init"], env=ENV, catch_exceptions=False)
            assert result.exit_code == 0
            gitignore = (Path(root) / ".gitignore").read_text(encoding="utf-8")
            assert ".evaling/" in gitignore
            assert ".evaling.secrets.yaml" in gitignore
            assert (Path(root) / ".evaling.secrets.yaml.example").is_file()

    @pytest.mark.parametrize(
        "provider,marker",
        [
            ("anthropic", "provider: anthropic"),
            ("openai", "provider: openai"),
            ("openai-compatible", "base_url: http://localhost:11434/v1"),
        ],
    )
    def test_provider_scaffolds(self, provider, marker):
        runner = CliRunner()
        with runner.isolated_filesystem() as root:
            from pathlib import Path

            result = runner.invoke(
                main, ["init", "--provider", provider], env=ENV, catch_exceptions=False
            )
            assert result.exit_code == 0, result.output
            config = (Path(root) / "eval.yaml").read_text(encoding="utf-8")
            assert marker in config
            assert "provider: mock" not in config

    def test_scaffolded_provider_config_validates(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            runner.invoke(main, ["init", "--provider", "anthropic"], env=ENV)
            # dry-run needs no API key, so this checks the scaffold is well-formed
            result = runner.invoke(main, ["validate"], env=ENV, catch_exceptions=False)
            assert result.exit_code == 0, result.output


class TestStreamingReads:
    def test_iter_results_matches_load_results(self, tmp_path):
        settings = make_settings(tmp_path)
        result = run_eval(make_config(tmp_path), settings)
        store = RunStore(settings.output_dir)
        streamed = [record.key for record in store.iter_results(result.run_id)]
        assert streamed == [record.key for record in store.load_results(result.run_id)]

    def test_iter_results_on_missing_run_is_empty(self, tmp_path):
        assert list(RunStore(tmp_path).iter_results("nope")) == []
