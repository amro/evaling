"""Case sources: pages in, cells out, nothing materialized."""

import asyncio

import pytest

from evaling.config import EvalConfig
from evaling.engine import dry_run, run_eval, select_matrix
from evaling.sources import (
    BaseCaseSource,
    CasePage,
    CaseSource,
    SourceError,
    close_source,
    iter_source_cases,
    load_source,
    source_count,
)
from evaling.storage import StorageError
from helpers import loop_ticks_during, make_settings

SOURCE_FILE = '''
from evaling import Case, CasePage

class Paging:
    """total cases, page_size per fetch, cursor = next index."""

    def __init__(self, total=10, fail_at=None):
        self.total = total
        self.fail_at = fail_at
        self.fetches = 0

    def fetch(self, cursor, limit):
        self.fetches += 1
        start = int(cursor or 0)
        if self.fail_at is not None and start >= self.fail_at:
            raise RuntimeError("source exploded")
        stop = min(start + limit, self.total)
        cases = [
            Case(id=f"c{i}", vars={"q": f"question {i}"}, expected="question")
            for i in range(start, stop)
        ]
        return CasePage(cases=cases, cursor=str(stop) if stop < self.total else None)

    def count(self):
        return self.total

    def close(self):
        self.closed = True


class AsyncPaging(Paging):
    async def fetch(self, cursor, limit):
        return Paging.fetch(self, cursor, limit)


class NoCount(Paging):
    count = None


def make_source(total=10, fail_at=None):
    return Paging(total=total, fail_at=fail_at)


def make_async(total=10):
    return AsyncPaging(total=total)


def make_no_count(total=10):
    return NoCount(total=total)


def not_a_source():
    return object()


def explodes():
    raise ValueError("bad params")
'''


@pytest.fixture
def source_dir(tmp_path):
    (tmp_path / "src.py").write_text(SOURCE_FILE, encoding="utf-8")
    return tmp_path


def source_config(base_dir, factory="make_source", params=None, **source_extra):
    config = EvalConfig.model_validate(
        {
            "models": [{"id": "mock", "provider": "mock"}],
            "variants": [{"name": "v1", "prompt": [{"role": "user", "content": "{{ q }}"}]}],
            "cases": {"source": f"src.py:{factory}", "params": params or {}, **source_extra},
            "scorecard": [{"criterion": "acc", "scorer": {"type": "contains"}}],
        }
    )
    config._base_dir = base_dir  # noqa: SLF001 - test fixture
    return config


class TestProtocol:
    def test_a_plain_class_satisfies_it(self):
        class Mine:
            def fetch(self, cursor, limit):
                return CasePage()

        assert isinstance(Mine(), CaseSource)

    def test_something_without_fetch_does_not(self):
        assert not isinstance(object(), CaseSource)

    def test_base_class_requires_fetch(self):
        with pytest.raises(TypeError):
            BaseCaseSource()

    def test_base_class_defaults_are_harmless(self):
        class Mine(BaseCaseSource):
            def fetch(self, cursor, limit):
                return CasePage()

        source = Mine()
        assert source.count() is None
        assert source.close() is None


class TestLoading:
    def test_loads_and_builds(self, source_dir):
        source = load_source("src.py:make_source", source_dir, {"total": 3})
        assert source.count() == 3

    def test_missing_file(self, source_dir):
        with pytest.raises(SourceError, match="file not found"):
            load_source("nope.py:make_source", source_dir)

    def test_missing_factory(self, source_dir):
        with pytest.raises(SourceError, match="no 'ghost'"):
            load_source("src.py:ghost", source_dir)

    def test_malformed_reference(self, source_dir):
        with pytest.raises(SourceError, match="must be"):
            load_source("src.py", source_dir)

    def test_bad_params(self, source_dir):
        with pytest.raises(SourceError, match="rejected its params"):
            load_source("src.py:make_source", source_dir, {"nonsense": 1})

    def test_factory_that_raises(self, source_dir):
        with pytest.raises(SourceError, match="raised ValueError"):
            load_source("src.py:explodes", source_dir)

    def test_object_without_fetch(self, source_dir):
        with pytest.raises(SourceError, match="no fetch"):
            load_source("src.py:not_a_source", source_dir)


class TestIteration:
    def collect(self, source, page_size=3, limit=None):
        async def go():
            return [case async for case in iter_source_cases(source, page_size, limit)]

        return asyncio.run(go())

    def test_walks_every_page(self, source_dir):
        source = load_source("src.py:make_source", source_dir, {"total": 10})
        cases = self.collect(source, page_size=3)
        assert [case.id for case in cases] == [f"c{i}" for i in range(10)]
        assert source.fetches == 4  # 3+3+3+1

    def test_async_fetch_works_the_same(self, source_dir):
        source = load_source("src.py:make_async", source_dir, {"total": 7})
        assert len(self.collect(source, page_size=2)) == 7

    def test_limit_stops_early_without_over_fetching(self, source_dir):
        source = load_source("src.py:make_source", source_dir, {"total": 100})
        cases = self.collect(source, page_size=10, limit=15)
        assert len(cases) == 15
        assert source.fetches == 2  # never asked for the other 85

    def test_empty_source(self, source_dir):
        source = load_source("src.py:make_source", source_dir, {"total": 0})
        assert self.collect(source) == []

    def test_a_stuck_cursor_is_an_error_not_an_infinite_loop(self):
        class Stuck:
            def fetch(self, cursor, limit):
                from evaling import Case

                return CasePage(cases=[Case(id="x", vars={})], cursor="always-the-same")

        with pytest.raises(SourceError, match="twice"):
            self.collect(Stuck(), page_size=1)

    def test_wrong_return_type(self):
        class Bad:
            def fetch(self, cursor, limit):
                return [1, 2, 3]

        with pytest.raises(SourceError, match="expected a CasePage"):
            self.collect(Bad())

    def test_wrong_case_type(self):
        class Bad:
            def fetch(self, cursor, limit):
                return CasePage(cases=[{"id": "x"}])

        with pytest.raises(SourceError, match="expected evaling.Case"):
            self.collect(Bad())

    def test_bad_page_size(self, source_dir):
        source = load_source("src.py:make_source", source_dir)
        with pytest.raises(SourceError, match="page_size"):
            self.collect(source, page_size=0)


class TestSyncSourcesDoNotBlockTheLoop:
    """A sync fetch/count/close doing real I/O must not stall in-flight calls.

    Regression: user methods ran directly on the event loop, so a source
    paging from a slow database froze every concurrent model call for the
    duration of each page.
    """

    class Slow:
        def fetch(self, cursor, limit):
            import time

            from evaling import Case

            time.sleep(0.2)
            return CasePage(cases=[Case(id="c1", vars={})])

        def count(self):
            import time

            time.sleep(0.2)
            return 1

        def close(self):
            import time

            time.sleep(0.2)

    def test_fetch(self):
        async def walk():
            return [case async for case in iter_source_cases(self.Slow(), 5, 1)]

        ticks, cases = asyncio.run(loop_ticks_during(walk()))
        assert len(cases) == 1
        assert ticks >= 3

    def test_count(self):
        ticks, total = asyncio.run(loop_ticks_during(source_count(self.Slow())))
        assert total == 1
        assert ticks >= 3

    def test_close(self):
        ticks, _ = asyncio.run(loop_ticks_during(close_source(self.Slow())))
        assert ticks >= 3


class TestRunningFromASource:
    def test_end_to_end(self, source_dir):
        config = source_config(source_dir, params={"total": 12}, page_size=5)
        result = run_eval(config, make_settings(source_dir))
        assert result.counts["total"] == 12
        assert result.counts["succeeded"] == 12
        assert result.aggregates["overall"]["cases"] == 12

    def test_matrix_multiplies_across_variants_and_models(self, source_dir):
        config = EvalConfig.model_validate(
            {
                "models": [
                    {"id": "m1", "provider": "mock"},
                    {"id": "m2", "provider": "mock"},
                ],
                "variants": [
                    {"name": "v1", "prompt": [{"role": "user", "content": "{{ q }}"}]},
                    {"name": "v2", "prompt": [{"role": "user", "content": "Q: {{ q }}"}]},
                ],
                "cases": {"source": "src.py:make_source", "params": {"total": 4}},
                "scorecard": [{"criterion": "acc", "scorer": {"type": "contains"}}],
            }
        )
        config._base_dir = source_dir  # noqa: SLF001
        result = run_eval(config, make_settings(source_dir))
        assert result.counts["total"] == 16  # 2 variants x 2 models x 4 cases

    def test_limit_caps_the_run(self, source_dir):
        config = source_config(source_dir, params={"total": 1000}, page_size=10, limit=25)
        result = run_eval(config, make_settings(source_dir))
        assert result.counts["total"] == 25

    def test_a_failing_source_surfaces_cleanly(self, source_dir):
        config = source_config(source_dir, params={"total": 10, "fail_at": 4}, page_size=2)
        with pytest.raises(RuntimeError, match="source exploded"):
            run_eval(config, make_settings(source_dir))

    def test_records_are_not_retained_for_source_runs(self, source_dir):
        """Size is unknown up front, so assume large rather than risk memory."""
        config = source_config(source_dir, params={"total": 5})
        result = run_eval(config, make_settings(source_dir))
        assert result.records_truncated is True
        assert result.records == []
        assert len(list(result.iter_records())) == 5


class TestRefusals:
    def test_resume_is_refused(self, source_dir):
        config = source_config(source_dir, params={"total": 4})
        settings = make_settings(source_dir)
        first = run_eval(config, settings)
        with pytest.raises(StorageError, match="resume is not supported"):
            run_eval(config, settings, resume_run_id=first.run_id)

    def test_case_filter_is_refused(self, source_dir):
        config = source_config(source_dir, params={"total": 4})
        with pytest.raises(Exception, match="--case cannot filter"):
            run_eval(config, make_settings(source_dir), case_filter=["c1"])

    def test_select_matrix_explains_itself(self, source_dir):
        with pytest.raises(Exception, match="fetched lazily"):
            select_matrix(source_config(source_dir))


class TestDryRun:
    def test_samples_the_first_page(self, source_dir):
        config = source_config(source_dir, params={"total": 500}, page_size=4)
        report = dry_run(config)
        assert len(report.cells) == 4  # 1 variant x 1 model x 4 sampled cases
        assert report.source_total == 500
        assert report.requests == 500  # count() known, so the real size
        assert not report.errors

    def test_reports_a_sample_when_the_total_is_unknown(self, source_dir):
        config = source_config(source_dir, factory="make_no_count", page_size=3)
        report = dry_run(config)
        assert report.sampled is True
        assert report.source_total is None

    def test_template_errors_are_caught_from_the_sample(self, source_dir):
        """A prompt referring to something the source doesn't supply."""
        config = EvalConfig.model_validate(
            {
                "models": [{"id": "mock", "provider": "mock"}],
                "variants": [
                    {"name": "v1", "prompt": [{"role": "user", "content": "{{ missing }}"}]}
                ],
                "cases": {"source": "src.py:make_source", "params": {"total": 5}},
                "scorecard": [{"criterion": "acc", "scorer": {"type": "contains"}}],
            }
        )
        config._base_dir = source_dir  # noqa: SLF001
        report = dry_run(config)
        assert report.errors and "missing" in report.errors[0]["error"]


class TestABoundedSourceIsConfirmedLikeAnyOtherRun:
    """The large-matrix ceiling applies wherever the cases come from.

    It lived only in the inline-cases branch, so a source-backed run with
    `limit: 150` started unprompted on a terminal while the MCP server refused
    the identical run — two surfaces disagreeing about the same ceiling.
    """

    def project(self, tmp_path, limit):
        (tmp_path / "src.py").write_text(
            "from evaling import Case, CasePage\n"
            "class S:\n"
            "    def fetch(self, cursor, limit):\n"
            "        start = int(cursor or 0)\n"
            "        rows = [Case(id=f'c{i}', vars={'q': str(i)}) "
            "for i in range(start, start + limit)]\n"
            "        return CasePage(cases=rows, cursor=str(start + limit))\n"
            "def make():\n"
            "    return S()\n",
            encoding="utf-8",
        )
        (tmp_path / "eval.yaml").write_text(
            "models: [{id: mock, provider: mock}]\n"
            "variants:\n  - name: v1\n"
            '    prompt: [{role: user, content: "{{ q }}"}]\n'
            f"cases: {{source: 'src.py:make', page_size: 50, limit: {limit}}}\n"
            'scorecard: [{criterion: acc, scorer: {type: contains, value: ""}}]\n',
            encoding="utf-8",
        )
        return tmp_path

    def counts_confirmed(self, path, *args, monkeypatch):
        """The cell count the run offered for confirmation, or None.

        CliRunner replaces sys.stdin, so the tty check inside the guard cannot
        be patched from out here; this tests that the source branch reaches
        the guard at all, which is the regression.
        """
        from click.testing import CliRunner

        import evaling.cli as cli_module

        seen = []
        monkeypatch.setattr(cli_module, "_confirm_large", lambda count, yes: seen.append(count))
        result = CliRunner().invoke(
            cli_module.main,
            ["-c", str(path / "eval.yaml"), "-o", str(path / "runs"), "run", *args],
            env={"EVALING_USER_CONFIG": "/nonexistent"},
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        return seen[0] if seen else None

    def test_a_source_backed_run_reaches_the_confirmation(self, tmp_path, monkeypatch):
        from evaling.engine import CONFIRM_THRESHOLD

        path = self.project(tmp_path, CONFIRM_THRESHOLD + 50)
        assert self.counts_confirmed(path, monkeypatch=monkeypatch) == CONFIRM_THRESHOLD + 50

    def test_an_inline_run_still_does_too(self, tmp_path, monkeypatch):
        (tmp_path / "eval.yaml").write_text(
            "models: [{id: mock, provider: mock}]\n"
            "variants:\n  - name: v1\n"
            '    prompt: [{role: user, content: "{{ q }}"}]\n'
            "cases: [{id: c1, vars: {q: a}}, {id: c2, vars: {q: b}}]\n"
            'scorecard: [{criterion: acc, scorer: {type: contains, value: ""}}]\n',
            encoding="utf-8",
        )
        assert self.counts_confirmed(tmp_path, monkeypatch=monkeypatch) == 2


class TestTheConfirmationThreshold:
    """The guard itself, away from CliRunner's stdin."""

    def guard(self, count, yes, tty, monkeypatch):
        import sys

        import evaling.cli as cli_module

        asked = []
        monkeypatch.setattr(cli_module.click, "confirm", lambda *a, **k: asked.append(a))
        monkeypatch.setattr(sys.stdin, "isatty", lambda: tty, raising=False)
        cli_module._confirm_large(count, yes)
        return bool(asked)

    def test_it_asks_above_the_threshold(self, monkeypatch):
        from evaling.engine import CONFIRM_THRESHOLD

        assert self.guard(CONFIRM_THRESHOLD, False, True, monkeypatch) is True

    def test_it_stays_quiet_below(self, monkeypatch):
        from evaling.engine import CONFIRM_THRESHOLD

        assert self.guard(CONFIRM_THRESHOLD - 1, False, True, monkeypatch) is False

    def test_yes_skips_it(self, monkeypatch):
        assert self.guard(10_000, True, True, monkeypatch) is False

    def test_no_tty_skips_it(self, monkeypatch):
        """CI would hang on a prompt; --max-cost is the ceiling there."""
        assert self.guard(10_000, False, False, monkeypatch) is False

    def test_an_unknown_count_skips_it(self, monkeypatch):
        """An unbounded source has no count; a different guard covers it."""
        assert self.guard(None, False, True, monkeypatch) is False
