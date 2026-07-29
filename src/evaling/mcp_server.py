"""MCP server: evaling's operations as tools an agent can drive.

The point is agent-driven prompt iteration — tweak a prompt, run the eval,
read the scores, repeat — so every response is written for a model reading it:
summaries by default, drill-down on request, pagination on anything unbounded,
and long text snipped with an explicit marker.

This module contains no eval logic. Each tool is a thin call into the core
library, exactly like the CLI. The functions below are plain and directly
testable; ``build_server()`` only registers them.
"""

from dataclasses import asdict
from pathlib import Path
from typing import Any

from evaling.config import load_config, resolve_settings
from evaling.config.loader import load_project_settings
from evaling.engine import dry_run, run_eval_async, select_matrix
from evaling.errors import EvalingError
from evaling.render import render_messages
from evaling.scoring import cell_summary, compare_aggregates, filter_failures
from evaling.storage import ResultRecord, RunStore, serialize_messages

#: Cap on inlined model output. Full text is one get_case_result away.
SNIPPET = 600
#: Page size for the unbounded listing (get_run detail="full").
PAGE_SIZE = 20

INSTRUCTIONS = """\
evaling compares prompt variants and models over test cases and scores them.

Typical loop: edit a prompt file, call run_eval, read the returned matrix,
then get_run(detail="failures") to see what broke and why. Use
get_case_result for one cell's full output, and compare_runs to check whether
an edit helped. render_prompt shows exactly what a case renders to without
calling any model.
"""


def _snip(text: str | None, limit: int = SNIPPET) -> str | None:
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return text[:limit] + f"… [{len(text) - limit} more chars; use get_case_result]"


def _settings(output_dir: str | None = None, config_path: str | None = None):
    """Same layering the CLI uses, so both see the same runs."""
    return resolve_settings(
        {"output_dir": Path(output_dir) if output_dir else None},
        load_project_settings(config_path or "eval.yaml"),
    )


def _store(output_dir: str | None = None, config_path: str | None = None) -> RunStore:
    return RunStore(_settings(output_dir, config_path).output_dir)


def _cell_row(record: ResultRecord, *, snippet: bool = True) -> dict[str, Any]:
    score, passed = cell_summary(record)
    row: dict[str, Any] = {
        "variant": record.variant,
        "model": record.model,
        "case_id": record.case_id,
        "passed": passed,
        "score": score,
    }
    if record.error:
        row["error"] = _snip(record.error) if snippet else record.error
    else:
        row["output"] = _snip(record.output) if snippet else record.output
    failed = {
        name: entry.get("detail") or entry.get("error") or "failed"
        for name, entry in record.scores.items()
        if entry.get("passed") is not True
    }
    if failed:
        row["failed_criteria"] = failed
    return row


def _run_summary(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": meta.get("id"),
        "label": meta.get("label"),
        "status": meta.get("status"),
        "started_at": meta.get("started_at"),
        "counts": meta.get("counts"),
        "totals": meta.get("totals"),
        "aggregates": meta.get("aggregates"),
        "gate": meta.get("gate"),
    }


# -- tools ---------------------------------------------------------------


async def run_eval_tool(
    config_path: str = "eval.yaml",
    models: list[str] | None = None,
    variants: list[str] | None = None,
    cases: list[str] | None = None,
    label: str | None = None,
    no_cache: bool = False,
    max_cost_usd: float | None = None,
    output_dir: str | None = None,
    on_progress=None,
) -> dict[str, Any]:
    """Run the eval matrix to completion and return its summary."""
    config = load_config(config_path)
    settings = resolve_settings(
        {
            "output_dir": Path(output_dir) if output_dir else None,
            "cache": False if no_cache else None,
        },
        config.settings,
    )
    variants_sel, models_sel, cases_sel = select_matrix(
        config, models=models, variants=variants, cases=cases
    )
    total = len(variants_sel) * len(models_sel) * len(cases_sel)

    done = 0

    def on_result(record: ResultRecord) -> None:
        nonlocal done
        done += 1
        if on_progress is not None:
            on_progress(done, total)

    result = await run_eval_async(
        config,
        settings,
        label=label,
        model_filter=models,
        variant_filter=variants,
        case_filter=cases,
        max_cost_usd=max_cost_usd,
        on_result=on_result,
    )
    summary = {
        "run_id": result.run_id,
        "counts": result.counts,
        "totals": result.totals,
        "aggregates": result.aggregates,
        "gate": asdict(result.gate) if result.gate else None,
        "warnings": result.warnings,
    }
    # result.records is empty above the retention cap, so stream from disk —
    # without materializing the run, which can be far larger than the failures.
    failures = filter_failures(result.iter_records())
    if failures:
        summary["failure_count"] = len(failures)
        summary["first_failures"] = [_cell_row(record) for record in failures[:5]]
        summary["hint"] = 'call get_run(detail="failures") for the rest'
    return summary


def get_run_tool(
    run_id: str,
    detail: str = "summary",
    page: int = 1,
    output_dir: str | None = None,
    config_path: str | None = None,
) -> dict[str, Any]:
    """Re-read a stored run: summary, its failures, or every cell (paginated)."""
    if detail not in ("summary", "failures", "full"):
        raise EvalingError(f"unknown detail {detail!r} (summary|failures|full)")
    store = _store(output_dir, config_path)
    resolved = store.resolve_ref(run_id)
    payload = _run_summary(store.load_meta(resolved))
    if detail == "summary":
        return payload

    records = store.load_results(resolved)
    selected = filter_failures(records) if detail == "failures" else records
    if page < 1:
        raise EvalingError("page must be >= 1")
    start = (page - 1) * PAGE_SIZE
    window = selected[start : start + PAGE_SIZE]
    payload["cells"] = [_cell_row(record) for record in window]
    payload["page"] = {
        "page": page,
        "page_size": PAGE_SIZE,
        "returned": len(window),
        "total": len(selected),
        "has_more": start + len(window) < len(selected),
    }
    return payload


def get_case_result_tool(
    run_id: str,
    variant: str,
    model: str,
    case_id: str,
    output_dir: str | None = None,
    config_path: str | None = None,
) -> dict[str, Any]:
    """One cell in full: untruncated output, every criterion, the prompt sent."""
    store = _store(output_dir, config_path)
    resolved = store.resolve_ref(run_id)
    for record in store.load_results(resolved):
        if (record.variant, record.model, record.case_id) == (variant, model, case_id):
            row = _cell_row(record, snippet=False)
            row["scores"] = record.scores
            row["messages"] = record.messages
            row["usage"] = {
                "input_tokens": record.input_tokens,
                "output_tokens": record.output_tokens,
                "cost_usd": record.cost_usd,
                "latency_ms": record.latency_ms,
                "cached": record.cached,
            }
            return row
    raise EvalingError(
        f"no cell {variant!r} × {model!r} × {case_id!r} in run {resolved} "
        '(call get_run(detail="full") to list them)'
    )


def compare_runs_tool(
    run_a: str, run_b: str, output_dir: str | None = None, config_path: str | None = None
) -> dict[str, Any]:
    """Score and pass-rate deltas between two runs."""
    store = _store(output_dir, config_path)
    metas = []
    for ref in (run_a, run_b):
        meta = store.load_meta(store.resolve_ref(ref))
        if not meta.get("aggregates"):
            raise EvalingError(f"run {meta['id']} has no aggregates (did it finish?)")
        metas.append(meta)
    diff = compare_aggregates(metas[0]["aggregates"], metas[1]["aggregates"])
    return {"a": metas[0]["id"], "b": metas[1]["id"], **diff}


def list_runs_tool(
    limit: int = 20, output_dir: str | None = None, config_path: str | None = None
) -> dict[str, Any]:
    """Stored runs, newest first."""
    store = _store(output_dir, config_path)
    runs = list(reversed(store.list_runs()))
    rows = []
    for meta in runs[: max(1, limit)]:
        overall = (meta.get("aggregates") or {}).get("overall") or {}
        rows.append(
            {
                "run_id": meta["id"],
                "label": meta.get("label"),
                "status": meta.get("status"),
                "started_at": meta.get("started_at"),
                "score": overall.get("score"),
                "pass_rate": overall.get("pass_rate"),
            }
        )
    return {"runs": rows, "total": len(runs), "baseline": store.get_baseline()}


def set_baseline_tool(
    run_id: str, output_dir: str | None = None, config_path: str | None = None
) -> dict[str, Any]:
    """Pin a run as the baseline used by regression gating."""
    store = _store(output_dir, config_path)
    resolved = store.resolve_ref(run_id)
    store.set_baseline(resolved)
    return {"baseline": resolved}


def render_prompt_tool(
    config_path: str = "eval.yaml",
    variant: str | None = None,
    case_id: str | None = None,
) -> dict[str, Any]:
    """Render a case's prompt exactly as a run would — no model is called.

    Without arguments it validates the whole matrix instead, which is the
    cheapest way to check a config edit before spending anything.
    """
    config = load_config(config_path)
    if variant is None or case_id is None:
        report = dry_run(config)
        return {
            "requests": report.requests,
            "render_errors": report.errors,
            "ok": not report.errors,
        }

    variants_sel, _, cases_sel = select_matrix(config, variants=[variant], cases=[case_id])
    from evaling.config.loader import resolve_prompt

    messages = resolve_prompt(variants_sel[0].prompt, config.base_dir)
    rendered = render_messages(messages, cases_sel[0], config.base_dir)
    return {
        "variant": variant,
        "case_id": case_id,
        "messages": serialize_messages(rendered, include_source=False),
    }


# -- server --------------------------------------------------------------


def build_server(output_dir: str | None = None, config_path: str | None = None):
    """Register the tools on a FastMCP server (import is lazy: optional dep)."""
    try:
        from mcp.server.fastmcp import Context, FastMCP
    except ImportError:  # pragma: no cover - exercised via the CLI's hint path
        raise EvalingError(
            "the MCP server needs the optional 'mcp' dependency: "
            "install with  pip install 'evaling[mcp]'"
        ) from None

    server = FastMCP("evaling", instructions=INSTRUCTIONS)

    @server.tool(description=run_eval_tool.__doc__)
    async def run_eval(
        ctx: Context,
        config_path: str = config_path or "eval.yaml",
        models: list[str] | None = None,
        variants: list[str] | None = None,
        cases: list[str] | None = None,
        label: str | None = None,
        no_cache: bool = False,
        max_cost_usd: float | None = None,
    ) -> dict[str, Any]:
        import asyncio
        import contextlib

        def on_progress(done: int, total: int) -> None:
            # Fire-and-forget: a progress notification must never fail a run.
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().create_task(_report(ctx, done, total))

        return await run_eval_tool(
            config_path=config_path,
            models=models,
            variants=variants,
            cases=cases,
            label=label,
            no_cache=no_cache,
            max_cost_usd=max_cost_usd,
            output_dir=output_dir,
            on_progress=on_progress,
        )

    @server.tool(description=get_run_tool.__doc__)
    def get_run(run_id: str, detail: str = "summary", page: int = 1) -> dict[str, Any]:
        return get_run_tool(run_id, detail, page, output_dir, config_path)

    @server.tool(description=get_case_result_tool.__doc__)
    def get_case_result(run_id: str, variant: str, model: str, case_id: str) -> dict[str, Any]:
        return get_case_result_tool(run_id, variant, model, case_id, output_dir, config_path)

    @server.tool(description=compare_runs_tool.__doc__)
    def compare_runs(run_a: str, run_b: str) -> dict[str, Any]:
        return compare_runs_tool(run_a, run_b, output_dir, config_path)

    @server.tool(description=list_runs_tool.__doc__)
    def list_runs(limit: int = 20) -> dict[str, Any]:
        return list_runs_tool(limit, output_dir, config_path)

    @server.tool(description=set_baseline_tool.__doc__)
    def set_baseline(run_id: str) -> dict[str, Any]:
        return set_baseline_tool(run_id, output_dir, config_path)

    @server.tool(description=render_prompt_tool.__doc__)
    def render_prompt(
        config_path_arg: str | None = None,
        variant: str | None = None,
        case_id: str | None = None,
    ) -> dict[str, Any]:
        return render_prompt_tool(config_path_arg or config_path or "eval.yaml", variant, case_id)

    return server


async def _report(ctx, done: int, total: int) -> None:
    import contextlib

    with contextlib.suppress(Exception):
        await ctx.report_progress(done, total, f"{done}/{total} cells")


def serve(output_dir: str | None = None, config_path: str | None = None) -> None:
    """Run the server on stdio (blocks)."""
    build_server(output_dir, config_path).run("stdio")
