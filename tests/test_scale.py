"""Guards on the things that make a large run possible.

Each of these encodes a bug that a 500-cell test could not have caught: work
that is O(total cells) instead of O(concurrency) looks fine at small sizes and
falls over at a hundred thousand.

These assert behaviour, not resource use. The measurements — memory flatness
and throughput — live in test_performance.py, which CI runs on its own.
"""

import asyncio
from functools import partial

import pytest

from evaling.concurrency import KeyedLocks, consume_bounded
from evaling.engine import MAX_RETAINED_RECORDS, run_eval
from evaling.providers import _REGISTRY
from evaling.providers.base import Completion
from evaling.providers.mock import MockProvider
from evaling.report import render_run_html
from evaling.scoring import Aggregator
from evaling.storage import ResultRecord, RunStore
from evaling.templating import _compile, render_text
from helpers import make_config, make_settings


class TaskCounter(MockProvider):
    """Records how many asyncio tasks exist while cells are in flight."""

    peak_tasks = 0

    async def complete(self, request):
        type(self).peak_tasks = max(type(self).peak_tasks, len(asyncio.all_tasks()))
        await asyncio.sleep(0)
        return Completion(text="ok", cost_usd=0.0)

    @classmethod
    def reset(cls):
        cls.peak_tasks = 0


class TestBoundedInFlight:
    def test_tasks_do_not_scale_with_cell_count(self, tmp_path, monkeypatch):
        """The matrix must not be materialized as one task per cell.

        asyncio.gather over every cell used to create all of them up front,
        which is ~1-3 KB of task apiece before a single request goes out.
        """
        monkeypatch.setitem(_REGISTRY, "mock", TaskCounter)
        TaskCounter.reset()
        cases = [{"id": f"c{i}", "vars": {"q": str(i)}} for i in range(600)]
        run_eval(make_config(tmp_path, cases=cases), make_settings(tmp_path, concurrency=4))
        # 4 workers + the odd internal task; nowhere near 600.
        assert TaskCounter.peak_tasks < 50, f"peak tasks {TaskCounter.peak_tasks} scales with cells"

    def test_consume_bounded_holds_at_most_limit(self):
        state = {"now": 0, "peak": 0}

        async def item():
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
            await asyncio.sleep(0.001)
            state["now"] -= 1
            return 1

        seen = []
        asyncio.run(consume_bounded((item for _ in range(200)), 5, seen.append))
        assert state["peak"] == 5
        assert len(seen) == 200

    def test_consume_bounded_streams_a_lazy_iterable(self):
        """The source must not be drained up front."""
        produced = []

        def factories():
            for i in range(100):
                produced.append(i)
                yield partial(asyncio.sleep, 0, result=i)

        seen = []
        asyncio.run(consume_bounded(factories(), 3, seen.append))
        assert len(seen) == 100 and len(produced) == 100

    def test_consume_bounded_propagates_errors(self):
        async def boom():
            raise RuntimeError("nope")

        with pytest.raises(RuntimeError, match="nope"):
            asyncio.run(consume_bounded([boom], 2, lambda _: None))

    @pytest.mark.parametrize("limit", [0, -1])
    def test_rejects_nonsense_limits(self, limit):
        with pytest.raises(ValueError, match="limit must be"):
            asyncio.run(consume_bounded([], limit, lambda _: None))


class TestRecordRetention:
    def test_small_runs_still_return_records(self, tmp_path):
        result = run_eval(make_config(tmp_path), make_settings(tmp_path))
        assert result.records and result.records_truncated is False

    def test_large_runs_do_not_hold_records(self, tmp_path, monkeypatch):
        monkeypatch.setattr("evaling.engine.MAX_RETAINED_RECORDS", 10)
        cases = [{"id": f"c{i}", "vars": {"q": str(i)}} for i in range(25)]
        result = run_eval(make_config(tmp_path, cases=cases), make_settings(tmp_path))

        # Empty, not partial: a partial list would silently give wrong answers.
        assert result.records == []
        assert result.records_truncated is True
        # ...but nothing is lost — the aggregates are complete and disk has it all.
        assert result.counts["total"] == 25
        assert result.aggregates["overall"]["cases"] == 25
        assert len(list(result.iter_records())) == 25

    def test_iter_records_matches_the_store(self, tmp_path):
        settings = make_settings(tmp_path)
        result = run_eval(make_config(tmp_path), settings)
        store = RunStore(settings.output_dir)
        assert [r.key for r in result.iter_records()] == [
            r.key for r in store.iter_results(result.run_id)
        ]

    def test_default_cap_is_generous_enough_for_ordinary_runs(self):
        assert MAX_RETAINED_RECORDS >= 10_000


class TestIncrementalAggregation:
    def make(self, variant, model, score, passed, error=None):
        record = ResultRecord(variant=variant, model=model, case_id=f"{variant}{score}")
        record.error = error
        if error is None:
            record.scores = {"acc": {"weight": 1.0, "score": score, "passed": passed}}
        return record

    def test_streaming_produces_the_arithmetic_it_should(self):
        """Checked against figures worked out by hand, not against `aggregate`.

        `aggregate` *is* an Aggregator fed in a loop, so comparing the two can
        only ever agree — an error in the shared arithmetic would pass.
        """
        records = [
            self.make("a", "m1", 1.0, True),
            self.make("a", "m1", 0.5, False),
            self.make("b", "m1", 0.0, False, error="boom"),
            self.make("b", "m2", 0.75, True),
        ]
        streamed = Aggregator()
        for record in records:
            streamed.add(record)
        result = streamed.result()

        overall = result["overall"]
        assert overall["cases"] == 4
        assert overall["errors"] == 1
        # Mean over the three scored cells; the errored one contributes 0.
        assert overall["score"] == pytest.approx((1.0 + 0.5 + 0.0 + 0.75) / 4)
        assert overall["pass_rate"] == pytest.approx(2 / 4)

        cells = {(cell["variant"], cell["model"]): cell for cell in result["matrix"]}
        assert set(cells) == {("a", "m1"), ("b", "m1"), ("b", "m2")}
        assert cells[("a", "m1")]["cases"] == 2
        assert cells[("a", "m1")]["score"] == pytest.approx(0.75)
        assert cells[("a", "m1")]["pass_rate"] == pytest.approx(0.5)
        assert cells[("b", "m1")]["errors"] == 1
        assert cells[("b", "m2")]["pass_rate"] == pytest.approx(1.0)

    def test_empty_aggregate_is_stable(self):
        empty = Aggregator().result()
        assert empty["overall"] == {"cases": 0, "score": 0.0, "pass_rate": 0.0, "errors": 0}
        assert empty["matrix"] == []

    def test_group_arithmetic(self):
        aggregator = Aggregator()
        for record in [self.make("a", "m", 1.0, True), self.make("a", "m", 0.0, False)]:
            aggregator.add(record)
        overall = aggregator.result()["overall"]
        assert overall == {"cases": 2, "score": 0.5, "pass_rate": 0.5, "errors": 0}


class TestTemplateCompilation:
    def test_a_template_compiles_once_however_often_it_renders(self):
        _compile.cache_clear()
        for i in range(50):
            assert render_text("hello {{ name }}", {"name": str(i)}) == f"hello {i}"
        info = _compile.cache_info()
        assert info.misses == 1, "template recompiled per render"
        assert info.hits == 49

    def test_distinct_templates_are_cached_separately(self):
        _compile.cache_clear()
        render_text("{{ a }}", {"a": 1})
        render_text("{{ b }}", {"b": 2})
        assert _compile.cache_info().misses == 2

    def test_cache_is_bounded(self):
        assert _compile.cache_info().maxsize is not None

    def test_syntax_errors_still_surface(self):
        from evaling.errors import TemplateError

        with pytest.raises(TemplateError, match="syntax error"):
            render_text("{{ unclosed", {})

    def test_undefined_still_surfaces_after_caching(self):
        from evaling.errors import TemplateError

        render_text("{{ known }}", {"known": 1})
        with pytest.raises(TemplateError, match="undefined"):
            render_text("{{ known }}", {})


class TestKeyedLocks:
    """One permanent lock per distinct cache key would be a slow leak."""

    def test_locks_are_released_when_the_last_waiter_leaves(self):
        locks = KeyedLocks()

        async def go():
            for i in range(500):
                async with locks(f"key-{i}"):
                    pass
            return len(locks)

        assert asyncio.run(go()) == 0

    def test_concurrent_callers_on_one_key_are_serialized(self):
        order = []

        async def go():
            locks = KeyedLocks()

            async def worker(name):
                async with locks("shared"):
                    order.append(f"enter-{name}")
                    await asyncio.sleep(0.01)
                    order.append(f"exit-{name}")

            await asyncio.gather(worker("a"), worker("b"))
            return len(locks)

        remaining = asyncio.run(go())
        # No interleaving: each enter is followed by its own exit.
        assert order[0].startswith("enter") and order[1].startswith("exit")
        assert order[1].split("-")[1] == order[0].split("-")[1]
        assert remaining == 0

    def test_a_failure_inside_the_lock_still_releases_it(self):
        locks = KeyedLocks()

        async def go():
            with pytest.raises(RuntimeError):
                async with locks("k"):
                    raise RuntimeError("boom")
            return len(locks)

        assert asyncio.run(go()) == 0

    def test_distinct_keys_do_not_block_each_other(self):
        locks = KeyedLocks()
        state = {"peak": 0, "now": 0}

        async def go():
            async def worker(key):
                async with locks(key):
                    state["now"] += 1
                    state["peak"] = max(state["peak"], state["now"])
                    await asyncio.sleep(0.01)
                    state["now"] -= 1

            await asyncio.gather(*(worker(f"k{i}") for i in range(5)))

        asyncio.run(go())
        assert state["peak"] == 5


class TestReportDegradesGracefully:
    """A full drill-down is ~1.5 KB of HTML per cell; 50k cells made a 75 MB page."""

    def records(self, n, failing=0):
        out = []
        for i in range(n):
            record = ResultRecord(variant="v", model="m", case_id=f"c{i}")
            passed = i >= failing
            record.output = "x" * 200
            record.scores = {
                "acc": {"weight": 1.0, "score": 1.0 if passed else 0.0, "passed": passed}
            }
            out.append(record)
        return out

    def meta(self, n):
        return {
            "id": "run1",
            "status": "complete",
            "started_at": "now",
            "counts": {"total": n, "succeeded": n, "failed": 0, "cached": 0},
            "totals": {"input_tokens": 1, "output_tokens": 1, "cost_usd": 0.0},
            "aggregates": {
                "overall": {"cases": n, "score": 1.0, "pass_rate": 1.0, "errors": 0},
                "matrix": [
                    {
                        "variant": "v",
                        "model": "m",
                        "cases": n,
                        "score": 1.0,
                        "pass_rate": 1.0,
                        "errors": 0,
                    }
                ],
            },
        }

    def test_small_runs_keep_the_full_drill_down(self):
        html = render_run_html(self.meta(5), self.records(5))
        assert "Large run" not in html
        for i in range(5):
            assert f"c{i}" in html

    def test_large_runs_are_summarized(self, monkeypatch):
        monkeypatch.setattr("evaling.report.MAX_DETAILED_CASES", 10)
        n = 40
        html = render_run_html(self.meta(n), self.records(n, failing=3))
        assert "Large run" in html
        assert "40" in html and "omits" in html
        # the failures are what survived
        assert "c0" in html and "c1" in html and "c2" in html
        # ...and the aggregate detail is still complete
        assert "Summary" in html
        assert "100.0%" in html

    def test_summary_mode_caps_the_failures_it_lists(self, monkeypatch):
        monkeypatch.setattr("evaling.report.MAX_DETAILED_CASES", 10)
        monkeypatch.setattr("evaling.report.SUMMARY_MODE_FAILURES", 5)
        html = render_run_html(self.meta(50), self.records(50, failing=50))
        assert html.count("<section class='case") == 5

    def test_report_size_stays_bounded(self, monkeypatch):
        monkeypatch.setattr("evaling.report.MAX_DETAILED_CASES", 100)
        small = len(render_run_html(self.meta(100), self.records(100, failing=10)))
        big = len(render_run_html(self.meta(5000), self.records(5000, failing=10)))
        # 50x the cells must not mean 50x the page.
        assert big < small * 2, f"report grew from {small} to {big} bytes"


class TestFailureStopsTheRest:
    """A fatal error must not leave workers issuing more paid calls.

    `asyncio.gather` propagates the first exception but does not cancel its
    siblings — they finish the current item and keep pulling new ones. In a run
    that means more model calls after the failure, made by tasks whose
    providers the caller is already closing.

    These use a loop that outlives the failure, because that is where the bug
    is observable: `asyncio.run` cancels stragglers during its own shutdown, so
    it hides the leak. The MCP server and any embedder of `run_eval_async` have
    a persistent loop.
    """

    def drive(self, limit=4, total=200, fail_at=2):
        """Run to first failure, then let the loop breathe and see what continues."""
        started, after_raise = [], []
        failed = {"yet": False}

        async def item(i):
            started.append(i)
            if failed["yet"]:
                after_raise.append(i)
            if i == fail_at:
                failed["yet"] = True
                raise RuntimeError("fatal")
            await asyncio.sleep(0.002)
            return i

        async def go():
            factories = (partial(item, i) for i in range(total))
            task = asyncio.create_task(consume_bounded(factories, limit, lambda _: None))
            with pytest.raises(RuntimeError, match="fatal"):
                await task
            # The loop keeps running, as it would in a server.
            await asyncio.sleep(0.2)

        asyncio.run(go())
        return started, after_raise

    def test_no_new_work_starts_after_the_failure(self):
        started, after_raise = self.drive()
        # Siblings already in flight may finish; nothing new may begin.
        assert len(after_raise) <= 4, (
            f"{len(after_raise)} cells ran after the failure — workers kept pulling"
        )
        assert len(started) <= 8, f"{len(started)} of 200 cells started"

    def test_handle_stops_being_called(self):
        handled = []

        async def ok():
            await asyncio.sleep(0.002)
            return "ok"

        async def boom():
            raise RuntimeError("fatal")

        async def go():
            def factories():
                yield boom
                for _ in range(200):
                    yield ok

            task = asyncio.create_task(consume_bounded(factories(), 3, handled.append))
            with pytest.raises(RuntimeError):
                await task
            await asyncio.sleep(0.2)

        asyncio.run(go())
        assert len(handled) <= 3, f"handle() ran {len(handled)} times after the failure"

    def test_a_clean_run_is_unaffected(self):
        seen = []
        asyncio.run(
            consume_bounded(
                (partial(asyncio.sleep, 0, result=i) for i in range(50)), 5, seen.append
            )
        )
        assert sorted(seen) == list(range(50))
