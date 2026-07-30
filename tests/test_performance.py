"""Performance guards, so the numbers stop being something measured by hand.

Memory flatness and throughput were both established once, by hand, and then
nothing watched them. A change that made the engine hold one record per cell,
or that serialized what used to overlap, would have looked exactly like a
passing test suite.

These are *scaling* assertions, not absolute ones. A shared CI runner has no
stable cells-per-second to compare against, but the shape of the curve is
stable: work per cell must not grow with the number of cells, and calls must
overlap. Bounds are deliberately loose — they exist to catch a 10x regression,
not a 10% one, and a guard that cries wolf gets deleted.
"""

import asyncio
import gc
import time
import tracemalloc

import pytest

from evaling.engine import run_eval
from evaling.providers import _REGISTRY
from evaling.providers.base import Completion
from evaling.providers.mock import MockProvider
from evaling.storage import ResultRecord
from helpers import make_config, make_settings

pytestmark = pytest.mark.perf

#: Stand-in for a model call. Long enough to dominate scheduling noise, short
#: enough that a few hundred cells stay under a second.
LATENCY_S = 0.005


class SlowMock(MockProvider):
    """A provider whose cost is a known, fixed wait — so ideal time is arithmetic."""

    async def complete(self, request):
        await asyncio.sleep(LATENCY_S)
        return Completion(text="ok", input_tokens=1, output_tokens=1, cost_usd=0.0)


def timed_run(tmp_path, monkeypatch, *, cells, concurrency):
    """Wall-clock seconds for a run of ``cells`` cells, and nothing else."""
    monkeypatch.setitem(_REGISTRY, "mock", SlowMock)
    config = make_config(
        tmp_path, cases=[{"id": f"c{i}", "vars": {"q": str(i)}} for i in range(cells)]
    )
    settings = make_settings(tmp_path, concurrency=concurrency)
    start = time.perf_counter()
    result = run_eval(config, settings)
    elapsed = time.perf_counter() - start
    assert result.counts["total"] == cells
    return elapsed


def ideal_s(cells, concurrency):
    return cells / concurrency * LATENCY_S


class TestThroughput:
    def test_a_run_stays_near_the_time_its_calls_cost(self, tmp_path, monkeypatch):
        """Total time must be dominated by the model calls, not by the engine.

        Rendering, scoring, and the append to results.jsonl all sit on a cell's
        path. If any of them stops being cheap — or stops being off-thread —
        this is where it shows.
        """
        cells, concurrency = 480, 8
        elapsed = timed_run(tmp_path, monkeypatch, cells=cells, concurrency=concurrency)
        ideal = ideal_s(cells, concurrency)
        assert elapsed < ideal * 5, (
            f"{cells} cells took {elapsed:.2f}s against an ideal of {ideal:.2f}s — "
            "the engine is now costing more than the calls it makes"
        )

    def test_per_cell_cost_does_not_grow_with_run_size(self, tmp_path, monkeypatch):
        """The regression this exists for: anything O(cells) per cell.

        A run that rescans its own results, re-sorts an accumulating list, or
        holds a growing structure looks fine at 200 cells and quadratic at
        200,000. Four times the cells must cost about four times the time.
        """
        small = timed_run(tmp_path / "small", monkeypatch, cells=200, concurrency=8)
        large = timed_run(tmp_path / "large", monkeypatch, cells=800, concurrency=8)
        assert large < small * 8, (
            f"200 cells took {small:.2f}s but 800 took {large:.2f}s — "
            "per-cell cost grows with the size of the run"
        )

    def test_calls_actually_overlap(self, tmp_path, monkeypatch):
        """Concurrency has to be real concurrency.

        A blocking call left on the event loop, or a lock held across an await,
        turns the worker pool into a queue. Cells and latency are identical
        here; only the limit differs.
        """
        serial = timed_run(tmp_path / "serial", monkeypatch, cells=96, concurrency=1)
        parallel = timed_run(tmp_path / "parallel", monkeypatch, cells=96, concurrency=8)
        assert serial > parallel * 3, (
            f"concurrency 1 took {serial:.2f}s and concurrency 8 took {parallel:.2f}s — "
            "calls are not overlapping"
        )


#: Bytes of case data per cell. Small on purpose: tracemalloc's overhead
#: scales with the size of the allocations it watches, and 8 KB of case text
#: per cell made the measurement cost twenty times the run it was measuring.
VAR_BYTES = 500


def build(tmp_path, cells):
    config = make_config(
        tmp_path, cases=[{"id": f"c{i}", "vars": {"q": "x" * VAR_BYTES}} for i in range(cells)]
    )
    return config, make_settings(tmp_path, concurrency=8)


def live_records() -> int:
    """How many ResultRecords are reachable right now.

    Strings aren't GC-tracked, but a ResultRecord is, and every scrap of case
    data a run could retain hangs off one. Counting them directly costs
    nothing and says exactly what the expensive measurement only implies.
    """
    gc.collect()
    return sum(1 for obj in gc.get_objects() if type(obj) is ResultRecord)


class TestMemoryDoesNotGrowWithCells:
    """A run past the retention cap must cost the same at 1,000 cells or 1,000,000.

    These lower :data:`MAX_RETAINED_RECORDS` rather than running a genuinely
    huge matrix, because that cap is the switch between the two behaviours and
    a test cannot afford 10,001 cells. Doing it the other way round was the
    bug in the original guard: it ran 4,000 cells against the real cap of
    10,000, so every record was being held *by design*, and the number it
    measured said nothing about the streaming path it was written to protect.
    """

    #: Small enough that the streaming path is what runs, large enough to keep
    #: the worker pool full.
    CAP = 50

    @pytest.fixture(autouse=True)
    def stream_records(self, monkeypatch):
        monkeypatch.setattr("evaling.engine.MAX_RETAINED_RECORDS", self.CAP)

    def test_no_record_outlives_the_cell_that_made_it(self, tmp_path):
        """The precise form of the property, measured by counting objects.

        A delta rather than an absolute count, so leftovers from earlier tests
        in the same process can't make this pass or fail on their own.
        """
        cells = 800
        config, settings = build(tmp_path, cells)
        before = live_records()
        result = run_eval(config, settings)

        assert result.counts["total"] == cells
        # Without this the test could silently drift back to measuring the
        # retained path, which is what made the original one vacuous.
        assert result.records_truncated, "the streaming path did not run"
        after = live_records()
        assert after - before == 0, (
            f"{after - before} of {cells} records are still reachable after the run"
        )

    def test_total_memory_is_flat_between_two_run_sizes(self, tmp_path):
        """The broader form: nothing else grows per cell either.

        The census above only sees records. This sees every allocation the run
        leaves behind, and comparing two sizes cancels out whatever fixed
        overhead a run has — so only growth that tracks cell count survives
        the subtraction.
        """
        growths = []
        for name, cells in (("small", 400), ("large", 1600)):
            config, settings = build(tmp_path / name, cells)
            gc.collect()
            tracemalloc.start()
            before = tracemalloc.take_snapshot()
            # Held across the snapshot, the way a caller holds it. Dropping the
            # result first made this measure nothing: anything reachable only
            # through RunResult — records being exactly that — was collected
            # before the snapshot was taken.
            result = run_eval(config, settings)
            gc.collect()
            after = tracemalloc.take_snapshot()
            tracemalloc.stop()
            assert result.records_truncated
            del result
            growths.append(sum(stat.size_diff for stat in after.compare_to(before, "filename")))

        small, large = growths
        # Compared against a floor, not a ratio: both are meant to be near
        # zero, and dividing by a number legitimately near zero is noise.
        # Holding every record puts this delta at ~1.3 MB.
        assert large - small < 400_000, (
            f"400 cells retained {small / 1e6:.2f} MB and 1600 retained {large / 1e6:.2f} MB — "
            "memory scales with cell count"
        )
