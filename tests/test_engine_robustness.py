"""Regressions for bugs found in the full-codebase review.

Each test here failed before its fix; they exist so the same class of problem
can't come back quietly.
"""

import asyncio
import json
from pathlib import Path

import pytest

from evaling.cache import ResponseCache
from evaling.config import Case, EvalConfig, Message, ModelSpec
from evaling.config.errors import ConfigError
from evaling.engine import run_eval
from evaling.providers import _REGISTRY
from evaling.providers.base import Completion, ProviderError
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

    @pytest.mark.parametrize("limit", [float("nan"), float("inf"), -1.0])
    def test_a_nonsense_ceiling_is_refused(self, tmp_path, probe, limit):
        """A NaN ceiling enforces nothing while still counting as a ceiling.

        `spent >= nan` is False forever, so the budget never trips — and
        because a ceiling was "given", it also satisfies the guard that stops
        an unbounded source from running unwatched. The one flag standing
        between a source and an unbounded bill, switched off by a typo.
        """
        probe.reset(cost=1.0)
        with pytest.raises(ConfigError, match="finite non-negative"):
            run_eval(eight_cases(tmp_path), make_settings(tmp_path), max_cost_usd=limit)

    def test_a_zero_ceiling_is_allowed_and_stops_everything(self, tmp_path, probe):
        probe.reset(cost=1.0)
        result = run_eval(eight_cases(tmp_path), make_settings(tmp_path), max_cost_usd=0.0)
        assert result.counts["total"] == 0

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

    def test_run_json_is_written_atomically(self, tmp_path, monkeypatch):
        """A write that dies partway must leave the previous file intact.

        Asserting only that the content arrived would pass against a plain
        `write_text`, which is the implementation this exists to rule out. So
        the rename is made to fail: a non-atomic write would have already
        overwritten the target by that point.
        """
        target = tmp_path / "run.json"
        write_json_atomic(target, {"a": 1})
        assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
        assert list(tmp_path.glob(".*tmp*")) == []  # no leftovers

        real_replace = Path.replace

        def die(self, other):
            raise OSError("interrupted before the rename")

        monkeypatch.setattr(Path, "replace", die)
        with pytest.raises(OSError):
            write_json_atomic(target, {"a": 2})
        monkeypatch.setattr(Path, "replace", real_replace)

        assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}, (
            "a failed write clobbered the previous run.json"
        )
        assert list(tmp_path.glob(".*tmp*")) == []  # and cleaned up after itself

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


class TestJudgeCallsAreGoverned:
    """A judge is a billable model call and must obey the same limits.

    JudgeScorer used to call its provider directly, so judge spend was
    invisible to --max-cost and to the judge model's own rate limits. A run
    with a $1.00 judge under a $0.05 cap completed for $20.20.
    """

    def config(self, tmp_path, cases=20, **judge_model):
        (tmp_path / "rubric.yaml").write_text("- role: user\n  content: 'g {{ output }}'\n")
        config = EvalConfig.model_validate(
            {
                "models": [
                    {"id": "m", "provider": "mock"},
                    {"id": "judge", "provider": "mock", "role": "judge", **judge_model},
                ],
                "variants": [{"name": "v", "prompt": [{"role": "user", "content": "{{ q }}"}]}],
                "cases": [{"id": f"c{i}", "vars": {"q": str(i)}} for i in range(cases)],
                "judges": {"j": {"model": "judge", "rubric": "rubric.yaml"}},
                "scorecard": [{"criterion": "q", "scorer": {"type": "llm-judge", "judge": "j"}}],
            }
        )
        config._base_dir = tmp_path  # noqa: SLF001
        return config

    def test_judge_spend_counts_against_max_cost(self, tmp_path, monkeypatch):
        class Priced(MockProvider):
            async def complete(self, request):
                await asyncio.sleep(0)
                # Cells are almost free; only judge spend can breach the cap, so
                # this cannot pass on matrix cost alone.
                cost = 1.00 if request.model.id == "judge" else 0.0001
                return Completion(text='{"score": 1.0}', cost_usd=cost)

        monkeypatch.setitem(_REGISTRY, "mock", Priced)
        result = run_eval(
            self.config(tmp_path),
            make_settings(tmp_path, concurrency=1),
            model_filter=["m"],
            max_cost_usd=0.05,
        )
        assert result.incomplete is True, (
            "the run finished all 20 cells. Cells cost $0.002 in total, so the $0.05 cap "
            "can only be breached by judge spend — which escaped the budget."
        )
        assert result.counts["total"] < 20

    def test_a_judge_stopped_by_the_ceiling_leaves_no_record(self, tmp_path, monkeypatch):
        """The cell is owed, not failed — so it must not be written at all.

        The ceiling can land between a cell's own call and its judge's. Scoring
        that criterion 0 wrote a record, which counted a cell that was never
        judged as a quality failure and marked it done, so a resume with a
        higher ceiling skipped it and the judge never ran. Permanent, and it
        gated the run.
        """

        class Priced(MockProvider):
            async def complete(self, request):
                await asyncio.sleep(0)
                return Completion(text='{"score": 1.0}', cost_usd=0.01)

        monkeypatch.setitem(_REGISTRY, "mock", Priced)
        # Each cell costs 0.01 and its judge another 0.01, so the ceiling is
        # crossed by a *judge* acquiring: two cells finish whole, the third
        # has its own call land and is then refused a judge.
        result = run_eval(
            self.config(tmp_path, cases=4),
            make_settings(tmp_path, concurrency=1),
            model_filter=["m"],
            max_cost_usd=0.05,
        )
        assert result.incomplete is True, "the ceiling was never reached"
        records = [
            record_from_dict(json.loads(line))
            for line in (result.path / "results.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert records, "no cell completed, so nothing here is being tested"
        poisoned = [
            (record.case_id, name, entry.get("error"))
            for record in records
            for name, entry in (record.scores or {}).items()
            if "max cost" in (entry.get("error") or "")
        ]
        assert not poisoned, f"cells recorded with a budget-skipped judge: {poisoned}"
        assert all(record.scores for record in records), (
            "a cell was recorded with no scores at all — the same bug in a different hat"
        )

    def test_a_resumed_run_counts_what_the_first_half_spent(self, tmp_path, monkeypatch):
        """The ceiling covers the whole run, not each attempt at it.

        The budget and the judge totals are seeded from the prior records on
        resume. Without that seeding, `--max-cost 0.05` would allow $0.05 per
        resume, so a run interrupted twice spends triple its ceiling — and the
        reported judge total would describe only the final segment.
        """

        class Priced(MockProvider):
            async def complete(self, request):
                await asyncio.sleep(0)
                return Completion(text='{"score": 1.0}', cost_usd=0.01)

        monkeypatch.setitem(_REGISTRY, "mock", Priced)
        config = self.config(tmp_path, cases=8)
        settings = make_settings(tmp_path, concurrency=1)
        first = run_eval(config, settings, model_filter=["m"], max_cost_usd=0.05)
        assert first.incomplete is True, "the first pass was not stopped by the ceiling"
        spent_first = first.totals["cost_usd"] + first.totals["judge_cost_usd"]
        calls_first = first.totals["judge_calls"]
        assert calls_first, "no judge ran, so there is no seeding to check"

        # Resumed against the same ceiling it already reached: everything is
        # already spent, so the resume must do nothing rather than start over.
        second = run_eval(
            config,
            settings,
            model_filter=["m"],
            max_cost_usd=0.05,
            resume_run_id=first.run_id,
        )
        spent_total = second.totals["cost_usd"] + second.totals["judge_cost_usd"]
        assert spent_total == pytest.approx(spent_first), (
            f"the resume spent {spent_total - spent_first:.4f} more against a ceiling "
            "the run had already reached"
        )
        assert second.totals["judge_calls"] == calls_first, (
            "the judge totals restarted from zero, so they describe the resume rather than the run"
        )

    def test_a_judge_that_retries_is_counted_once_and_billed_once(self, tmp_path, monkeypatch):
        """Only matrix-model retries were tested; the judge path has its own.

        `_billed_call` counts before the attempt so a failed call still shows
        as "something was called", then releases the budget slot with the cost
        of whichever attempt succeeded. Counting inside the retry loop, or
        releasing per attempt, would inflate both.
        """

        class Flaky(MockProvider):
            attempts = 0

            async def complete(self, request):
                await asyncio.sleep(0)
                if request.model.id != "judge":
                    return Completion(text="answer", cost_usd=0.0)
                type(self).attempts += 1
                if type(self).attempts < 3:
                    raise ProviderError("judge is warming up", retryable=True)
                return Completion(text='{"score": 1.0}', cost_usd=0.02)

        Flaky.attempts = 0
        monkeypatch.setitem(_REGISTRY, "mock", Flaky)
        result = run_eval(
            self.config(tmp_path, cases=1, max_retries=3),
            make_settings(tmp_path, concurrency=1),
            model_filter=["m"],
        )
        assert Flaky.attempts == 3, "the judge did not actually retry"
        assert result.counts["succeeded"] == 1
        # One judge call, however many attempts it took, and the cost of the
        # attempt that landed — not one per attempt.
        assert result.totals["judge_calls"] == 1
        assert result.totals["judge_cost_usd"] == pytest.approx(0.02)

    def test_a_judge_that_never_succeeds_frees_its_budget_slot(self, tmp_path, monkeypatch):
        """A failed call tells us nothing about price, so it must not be
        mistaken for an unpriced one — that made --max-cost warn it could not
        be enforced and serialize the run."""

        class AlwaysFails(MockProvider):
            async def complete(self, request):
                await asyncio.sleep(0)
                if request.model.id != "judge":
                    return Completion(text="answer", cost_usd=0.01)
                raise ProviderError("judge is down", retryable=False)

        monkeypatch.setitem(_REGISTRY, "mock", AlwaysFails)
        result = run_eval(
            self.config(tmp_path, cases=3),
            make_settings(tmp_path, concurrency=1),
            model_filter=["m"],
            max_cost_usd=1.0,
        )
        # Every cell ran and every judge failed its criterion. The run is not
        # stopped, not serialized, and not warned about as unenforceable.
        assert result.counts["total"] == 3
        assert result.totals["judge_calls"] == 3
        assert result.totals["judge_cost_usd"] == 0.0
        assert not any("could not be enforced" in warning for warning in result.warnings)

    def test_judge_spend_is_reported(self, tmp_path, monkeypatch):
        """Enforcing it but not reporting it makes the run's own totals lie."""

        class Priced(MockProvider):
            async def complete(self, request):
                await asyncio.sleep(0)
                cost = 1.00 if request.model.id == "judge" else 0.01
                return Completion(text='{"score": 1.0}', cost_usd=cost)

        monkeypatch.setitem(_REGISTRY, "mock", Priced)
        result = run_eval(
            self.config(tmp_path, cases=3), make_settings(tmp_path), model_filter=["m"]
        )
        assert result.totals["judge_cost_usd"] == pytest.approx(3.00)
        assert result.totals["cost_usd"] == pytest.approx(3.03)  # cells + judges

    def test_judge_obeys_its_models_concurrency_cap(self, tmp_path, monkeypatch):
        """Observable proof the judge goes through its model's limiter."""
        state = {"now": 0, "peak": 0}

        class Tracked(MockProvider):
            async def complete(self, request):
                if request.model.id == "judge":
                    state["now"] += 1
                    state["peak"] = max(state["peak"], state["now"])
                    await asyncio.sleep(0.02)
                    state["now"] -= 1
                    return Completion(text='{"score": 1.0}', cost_usd=0.0)
                await asyncio.sleep(0)
                return Completion(text="ok", cost_usd=0.0)

        monkeypatch.setitem(_REGISTRY, "mock", Tracked)
        config = self.config(tmp_path, cases=6, max_concurrency=1)
        run_eval(config, make_settings(tmp_path, concurrency=6), model_filter=["m"])
        assert state["peak"] == 1, (
            f"{state['peak']} judge calls ran at once despite max_concurrency: 1 — "
            "the judge bypassed its model's limiter"
        )


class TestBudgetDistinguishesFailureFromUnpriced:
    """A call that raised tells us nothing about pricing.

    Treating it as unpriced warned that --max-cost 'could not be enforced' on a
    fully-priced run that merely hit one transient error, and marked the budget
    knowable with no cost data.
    """

    def test_a_failed_call_does_not_claim_the_model_is_unpriced(self, tmp_path, monkeypatch):
        class FlakyPriced(MockProvider):
            calls = 0

            async def complete(self, request):
                type(self).calls += 1
                await asyncio.sleep(0)
                if type(self).calls == 1:
                    raise ProviderError("upstream hiccup", retryable=False)
                return Completion(text="ok", cost_usd=0.001)

        monkeypatch.setitem(_REGISTRY, "mock", FlakyPriced)
        cases = [{"id": f"c{i}", "vars": {"q": str(i)}} for i in range(5)]
        result = run_eval(
            make_config(tmp_path, cases=cases),
            make_settings(tmp_path, concurrency=1),
            max_cost_usd=1.00,
        )
        assert result.counts["failed"] == 1
        unpriced = [w for w in result.warnings if "pricing" in w]
        assert not unpriced, f"a failed call was reported as an unpriced model: {unpriced}"


class TestACappedRunCanBeFinished:
    """Hitting the cost ceiling leaves cells owed, not failed.

    The old behaviour recorded every unattempted cell as a failure, computed a
    pass rate over cells that never ran, finalized the run as `complete`, and
    then refused to resume it — so the skipped half could never be finished
    and the numbers looked like a quality collapse.
    """

    def config(self, tmp_path, cases=12):
        return make_config(
            tmp_path,
            models=[{"id": "m", "provider": "mock", "params": {"cost": 0.01}}],
            cases=[{"id": f"c{i}", "vars": {"q": str(i)}} for i in range(cases)],
        )

    def capped(self, tmp_path):
        settings = make_settings(tmp_path, concurrency=1)
        result = run_eval(self.config(tmp_path), settings, max_cost_usd=0.03)
        return settings, result

    def test_the_run_stops_instead_of_failing_every_remaining_cell(self, tmp_path):
        _, result = self.capped(tmp_path)
        assert result.counts["total"] < 12
        assert result.incomplete is True

    def test_the_scores_cover_only_what_ran(self, tmp_path):
        """A pass rate computed over cells that were never attempted is a lie."""
        _, result = self.capped(tmp_path)
        assert result.aggregates["overall"]["cases"] == result.counts["total"]

    def test_it_says_what_happened(self, tmp_path):
        _, result = self.capped(tmp_path)
        assert any("cost ceiling" in warning for warning in result.warnings)

    def test_the_run_is_not_marked_complete(self, tmp_path):
        settings, result = self.capped(tmp_path)
        meta = RunStore(settings.output_dir).load_meta(result.run_id)
        assert meta["status"] == "incomplete"

    def test_a_skipped_cell_leaves_no_record(self, tmp_path):
        """It was never attempted, so it is not a failure and not done.

        Writing a record for it counted it against the pass rate and — worse —
        marked it complete for a later resume, leaving that one cell
        permanently failed.
        """
        settings, result = self.capped(tmp_path)
        records = RunStore(settings.output_dir).load_results(result.run_id)
        assert not [r for r in records if r.error and "max cost" in r.error]
        assert len(records) == result.counts["total"]

    def test_a_higher_ceiling_finishes_it(self, tmp_path):
        settings, first = self.capped(tmp_path)
        resumed = run_eval(
            self.config(tmp_path), settings, resume_run_id=first.run_id, max_cost_usd=10.0
        )
        assert resumed.run_id == first.run_id
        assert resumed.counts["total"] == 12
        assert resumed.incomplete is False
        records = RunStore(settings.output_dir).load_results(first.run_id)
        keys = [record.key for record in records]
        assert len(keys) == len(set(keys)) == 12, "resume duplicated cells"
        # And every cell really ran, including the one the ceiling stopped at.
        assert not [r for r in records if r.error], "a cell was left failed by the ceiling"

    def test_an_uncapped_run_is_unaffected(self, tmp_path):
        result = run_eval(self.config(tmp_path), make_settings(tmp_path))
        assert result.incomplete is False
        assert result.counts["total"] == 12
        assert not result.warnings

    def test_the_cli_exits_nonzero(self, tmp_path):
        """An incomplete run is not a pass; it did not evaluate what was asked."""
        from click.testing import CliRunner

        from evaling.cli import main

        (tmp_path / "eval.yaml").write_text(
            "models: [{id: m, provider: mock, params: {cost: 0.01}}]\n"
            "variants:\n  - name: v1\n"
            '    prompt: [{role: user, content: "{{ q }}"}]\n'
            "cases: [" + ", ".join(f"{{id: c{i}, vars: {{q: '{i}'}}}}" for i in range(12)) + "]\n"
            'scorecard: [{criterion: acc, scorer: {type: contains, value: ""}}]\n',
            encoding="utf-8",
        )
        result = CliRunner().invoke(
            main,
            [
                "-c",
                str(tmp_path / "eval.yaml"),
                "-o",
                str(tmp_path / "runs"),
                "run",
                "--concurrency",
                "1",
                "--max-cost",
                "0.03",
            ],
            env={"EVALING_USER_CONFIG": "/nonexistent"},
            catch_exceptions=False,
        )
        assert result.exit_code == 1, result.output
        assert "cost ceiling" in result.output
