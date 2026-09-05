"""MCP must not reconstruct an entire run while summarizing or paging it."""

import asyncio
import json
import weakref
from types import SimpleNamespace

import pytest

from evaling import mcp_server
from evaling.storage import ResultRecord, StorageError

TOTAL = 245


@pytest.fixture
def stream(monkeypatch):
    alive = weakref.WeakValueDictionary()
    seen = []

    def records(*args):
        for i in range(TOTAL):
            record = ResultRecord(
                variant="v",
                model="m",
                case_id=str(i),
                output=f"{i}:" + "x" * 1000,
                scores={"grade": {"score": 1 if i % 3 == 0 else 0, "passed": i % 3 == 0}},
            )
            alive[i] = record
            # Deterministic retention guard rather than a timing/RSS bound.
            # The iterator, caller, and single-cell lookup may each hold one.
            assert len(alive) <= 3, "MCP retained records outside the requested window"
            seen.append(i)
            yield record

    def load_results(*args):
        pytest.fail("MCP must stream results instead of materializing them")

    store = SimpleNamespace(
        resolve_ref=lambda ref: "run",
        load_meta=lambda ref: {"id": "run"},
        iter_results=records,
        load_results=load_results,
    )
    monkeypatch.setattr(mcp_server, "_store", lambda *args: store)
    return records, seen


def test_summary_counts_all_failures_but_retains_only_five(tmp_path, monkeypatch, stream):
    records, seen = stream
    path = tmp_path / "eval.yaml"
    path.write_text(
        json.dumps(
            {
                "models": [{"id": "m", "provider": "mock"}],
                "variants": [{"name": "v", "prompt": [{"role": "user", "content": "hello"}]}],
                "cases": [{"id": "c"}],
                "scorecard": [{"criterion": "ok", "scorer": {"type": "exact"}}],
            }
        ),
        encoding="utf-8",
    )

    async def run(*args, **kwargs):
        return SimpleNamespace(
            run_id="run",
            counts={"total": TOTAL},
            totals={},
            aggregates={},
            gate=None,
            warnings=[],
            selection=None,
            stopped_early=False,
            incomplete=False,
            iter_records=records,
        )

    monkeypatch.setattr(mcp_server, "run_eval_async", run)
    result = asyncio.run(mcp_server.run_eval_tool(config_path=str(path)))
    failures = [i for i in range(TOTAL) if i % 3]
    assert result["failure_count"] == len(failures)
    assert [row["case_id"] for row in result["first_failures"]] == list(map(str, failures[:5]))
    assert seen == list(range(TOTAL))


@pytest.mark.parametrize("detail", ["full", "failures"])
@pytest.mark.parametrize("page", [1, 2, 9, 99])
def test_pages_stream_with_exact_totals_and_windows(stream, detail, page):
    _, seen = stream
    result = mcp_server.get_run_tool("run", detail=detail, page=page)
    selected = [i for i in range(TOTAL) if detail == "full" or i % 3]
    start = (page - 1) * mcp_server.PAGE_SIZE
    window = selected[start : start + mcp_server.PAGE_SIZE]
    assert [row["case_id"] for row in result["cells"]] == list(map(str, window))
    assert result["page"] == {
        "page": page,
        "page_size": mcp_server.PAGE_SIZE,
        "returned": len(window),
        "total": len(selected),
        "has_more": start + len(window) < len(selected),
    }
    assert seen == list(range(TOTAL))


def test_cell_lookup_streams_and_returns_full_output(stream):
    _, seen = stream
    result = mcp_server.get_case_result_tool("run", "v", "m", "1")
    assert result["output"] == "1:" + "x" * 1000
    assert result["scores"]["grade"]["passed"] is False
    assert seen == list(range(TOTAL))


@pytest.mark.parametrize("lookup", ["full", "failures", "cell"])
def test_pagination_and_lookup_do_not_hide_late_corruption(stream, lookup):
    records, _ = stream

    def corrupt(*args):
        yield from records()
        raise StorageError("corrupt results")

    store = mcp_server._store()
    store.iter_results = corrupt
    with pytest.raises(StorageError, match="corrupt results"):
        if lookup == "cell":
            mcp_server.get_case_result_tool("run", "v", "m", "1")
        else:
            mcp_server.get_run_tool("run", detail=lookup)
