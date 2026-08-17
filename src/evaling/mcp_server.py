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

from evaling import __version__
from evaling.config import load_config, resolve_settings
from evaling.config.errors import ConfigError
from evaling.config.loader import load_project_settings
from evaling.config.schema import CaseSourceRef
from evaling.engine import (
    dry_run,
    run_eval_async,
    select_matrix,
    select_variants_models,
)
from evaling.errors import EvalingError
from evaling.render import render_messages
from evaling.scoring import cell_summary, compare_aggregates, filter_failures, selection_note
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

Before writing or editing an eval.yaml, read the evaling://config-schema
resource. It is generated from this evaling, so it is the schema actually
enforced — and the config rejects unknown keys, which makes a guessed one a
load error rather than a setting that quietly does nothing.
"""

#: URI of the config schema resource.
CONFIG_SCHEMA_URI = "evaling://config-schema"


def _snip(text: str | None, limit: int = SNIPPET) -> str | None:
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return text[:limit] + f"… [{len(text) - limit} more chars; use get_case_result]"


def _settings(output_dir: str | None = None, config_path: str | None = None):
    """Same layering the CLI uses, so both see the same runs."""
    target = Path(config_path or "eval.yaml")
    return resolve_settings(
        {"output_dir": Path(output_dir) if output_dir else None},
        load_project_settings(target),
        base_dir=target.resolve().parent if target.is_file() else None,
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
    sample: int | None = None,
    sample_seed: int | None = None,
    label: str | None = None,
    no_cache: bool = False,
    max_cost_usd: float | None = None,
    fail_fast: bool = False,
    output_dir: str | None = None,
    on_progress=None,
) -> dict[str, Any]:
    """Run the eval matrix to completion and return its summary."""
    if sample_seed is not None and sample is None:
        # The CLI refuses this for the same reason: accepting it silently
        # would look like the draw had been pinned.
        raise ConfigError("sample_seed has no effect without sample")
    config = load_config(config_path)
    settings = resolve_settings(
        {
            "output_dir": Path(output_dir) if output_dir else None,
            "cache": False if no_cache else None,
        },
        config.settings,
        base_dir=config.base_dir,
    )
    if isinstance(config.cases, CaseSourceRef):
        # A source is walked lazily, so there is no cell count to compute — and
        # asking select_matrix for one raises. The CLI has the same split.
        if cases:
            raise ConfigError(
                "cases cannot filter a source-backed run: cases are fetched lazily, so "
                "evaling does not know the ids in advance. Filter inside your source, "
                "or set `limit` in the config to take fewer."
            )
        if sample is not None:
            raise ConfigError(
                "sample cannot narrow a source-backed run: cases are fetched lazily, so "
                "there is no population to draw from. Set `limit` in the config to take "
                "fewer, or sample inside your source."
            )
        if config.cases.limit is None and max_cost_usd is None:
            # Nothing here can interrupt a run, and the size is unknown even
            # to the config's author, so this is the one case that is refused
            # rather than reported. The CLI refuses it too when not at a tty.
            raise ConfigError(
                "this config fetches cases from a source with no `limit`, so the number "
                "of model calls is whatever the source returns — and nothing here can "
                "interrupt it. Set `limit` in the config, or pass max_cost_usd."
            )
        variants_sel, models_sel = select_variants_models(config, models=models, variants=variants)
        total = (
            len(variants_sel) * len(models_sel) * config.cases.limit if config.cases.limit else None
        )
    else:
        variants_sel, models_sel, cases_sel = select_matrix(
            config, models=models, variants=variants, cases=cases
        )
        # Size only: the draw itself happens in the engine, which records the
        # seed it used. Any draw of the same size gives the same total.
        selected = min(sample, len(cases_sel)) if sample is not None else len(cases_sel)
        total = len(variants_sel) * len(models_sel) * selected

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
        sample=sample,
        sample_seed=sample_seed,
        max_cost_usd=max_cost_usd,
        fail_fast=fail_fast,
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
    if result.selection:
        # The seed is what lets the agent repeat the draw on the next call.
        summary["selection"] = result.selection
    if result.stopped_early:
        # Otherwise the counts read as a whole matrix that happened to be small.
        summary["stopped_early"] = True
    if result.incomplete:
        summary["incomplete"] = True
        summary["hint"] = "the cost ceiling stopped this run; resume it with a higher one"
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
    payload = {"a": metas[0]["id"], "b": metas[1]["id"], **diff}
    caveat = selection_note(metas[0], metas[1])
    if caveat:
        payload["warning"] = caveat
    return payload


def list_runs_tool(
    limit: int = 20, output_dir: str | None = None, config_path: str | None = None
) -> dict[str, Any]:
    """Stored runs, newest first."""
    store = _store(output_dir, config_path)
    runs = list(reversed(store.list_runs()))
    rows = []
    for meta in runs[: max(1, limit)]:  # clamped; the CLI clamps identically
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
    if config.privacy.no_look:
        # The whole point of this tool is to show rendered case content, which
        # is precisely what the mode exists to withhold. Refused rather than
        # redacted: a render with the case data removed answers no question.
        raise ConfigError(
            "render_prompt cannot show a no-look config's prompts: rendering a case is "
            "showing its data, which is what no-look mode prevents. Use a config over "
            "data you are allowed to read to check a template."
        )
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


def installed_mcp_version() -> str | None:
    """The installed `mcp` version, or None if the package isn't there.

    Read from distribution metadata rather than by importing, so an `mcp` whose
    import is broken still reports the version it claims to be. Importing to
    find out is what makes "not installed" and "installed but unusable" look
    alike, which is the confusion this exists to avoid.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("mcp")
    except PackageNotFoundError:
        return None


def _unusable_mcp_message(err: ImportError) -> str:
    """Say which of three things is wrong, since the remedy differs for each.

    All of them arrive as an ImportError from the same line. Blaming the one we
    can name — "install mcp" — sends someone already running 1.x to tick a
    ticked box, and tells someone whose 2.x install is broken to reinstall what
    is already there. The third case can't be diagnosed from here, so it hands
    back the import error instead of guessing.
    """
    found = installed_mcp_version()
    if found is None:
        return (
            "the MCP server needs the optional 'mcp' dependency: "
            "install with  pip install 'evaling[mcp]'"
        )
    major = found.split(".", 1)[0]
    if major.isdigit() and int(major) < 2:
        return (
            f"the MCP server needs mcp 2.0 or newer, but mcp {found} is installed: "
            "upgrade with  pip install --upgrade 'evaling[mcp]'"
        )
    return f"mcp {found} is installed but could not be loaded: {err}"


def config_schema_resource() -> dict[str, Any]:
    """JSON Schema for eval.yaml, as the installed evaling enforces it.

    Generated from the config models rather than written beside them. A
    hand-kept copy can describe a schema this version does not implement, and
    an agent has no way to tell which of the two it is holding; a generated one
    cannot disagree with the loader that will reject the file.
    """
    from evaling.config.schema import EvalConfig

    schema = EvalConfig.model_json_schema()
    schema["title"] = "evaling eval.yaml"
    schema["description"] = (
        f"Configuration schema for evaling {__version__}. Unknown keys are rejected "
        "everywhere except scorer parameters, so a typo fails at load time. Relative "
        "paths resolve against the directory holding the config file."
    )
    return schema


def build_server(output_dir: str | None = None, config_path: str | None = None):
    """Register the tools on an MCP server (import is lazy: optional dep)."""
    # Bound once here so the tool wrappers below can take `config_path` as
    # their own parameter name without shadowing the server's default.
    default_config = config_path
    try:
        from mcp.server.mcpserver import Context, MCPServer
    except ImportError as err:  # pragma: no cover - exercised via the CLI's hint path
        # Chained, not suppressed: for the third case above the traceback is
        # the only thing that says which module actually failed.
        raise EvalingError(_unusable_mcp_message(err)) from err

    # Without `version`, the server reports an empty one. An agent asking what
    # it is connected to should hear evaling's.
    server = MCPServer("evaling", version=__version__, instructions=INSTRUCTIONS)

    @server.resource(
        CONFIG_SCHEMA_URI,
        name="eval.yaml schema",
        description=(
            "JSON Schema for eval.yaml, generated from this evaling. Read it before "
            "writing or editing a config: unknown keys are rejected at load time."
        ),
        mime_type="application/json",
    )
    def config_schema() -> dict[str, Any]:
        return config_schema_resource()

    @server.tool(description=run_eval_tool.__doc__)
    async def run_eval(
        ctx: Context,
        config_path: str = config_path or "eval.yaml",
        models: list[str] | None = None,
        variants: list[str] | None = None,
        cases: list[str] | None = None,
        sample: int | None = None,
        sample_seed: int | None = None,
        label: str | None = None,
        no_cache: bool = False,
        max_cost_usd: float | None = None,
        fail_fast: bool = False,
    ) -> dict[str, Any]:
        import asyncio
        import contextlib

        def on_progress(done: int, total: int | None) -> None:
            # Fire-and-forget: a progress notification must never fail a run.
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().create_task(_report(ctx, done, total))

        return await run_eval_tool(
            config_path=config_path,
            models=models,
            variants=variants,
            cases=cases,
            sample=sample,
            sample_seed=sample_seed,
            label=label,
            no_cache=no_cache,
            max_cost_usd=max_cost_usd,
            fail_fast=fail_fast,
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
        config_path: str | None = None,
        variant: str | None = None,
        case_id: str | None = None,
    ) -> dict[str, Any]:
        # Named `config_path` like every other tool. It used to carry the
        # wrapper's own variable name, so an agent passing the argument that
        # works everywhere else got told it was unknown.
        return render_prompt_tool(config_path or default_config or "eval.yaml", variant, case_id)

    _reject_unknown_arguments(server)
    return server


def _reject_unknown_arguments(server) -> None:
    """Make a tool take exactly the arguments it declares, and refuse the rest.

    Without this, an agent that misspells `config_path` gets a successful run
    of the *default* config instead of an error — the worst kind of wrong,
    because it looks like it worked, on the one tool that spends money.

    Two halves. The generated schema permits any extra property, so each tool's
    schema is marked closed. And nothing checks arguments against that schema —
    the SDK drops what a tool didn't declare — so ``call_tool`` is wrapped to
    refuse an unknown argument rather than silently discard it.

    ``call_tool`` is the SDK's public entry point, and its own request handler
    dispatches through it, so wrapping the instance covers every caller.

    Reading the declared names needs the tool manager's internals, which is why
    pyproject pins ``mcp`` below 3. If those internals move anyway, the schemas
    silently stop being closed *and* every call carrying an argument is refused
    — this fails shut, not open, but a server that refuses everything is still
    a broken one. Two tests over the real protocol cover both halves, so the
    SDK release that moves them breaks CI rather than a user's server.
    """
    manager = getattr(server, "_tool_manager", None)
    tools = getattr(manager, "_tools", None)
    if not isinstance(tools, dict):  # pragma: no cover - SDK internals moved
        tools = {}
    if tools:
        for tool in tools.values():
            schema = getattr(tool, "parameters", None)
            if isinstance(schema, dict):
                schema.setdefault("additionalProperties", False)
    # Reject arguments the tool doesn't declare, and nothing else.
    #
    # Full jsonschema validation against the closed schema would do this, but
    # it would run ahead of the SDK's `pre_parse_json` — which exists because
    # some clients send list and object arguments as JSON-encoded strings.
    # Validating everything would therefore refuse calls those clients make
    # correctly today, to fix a different problem. Checking names only keeps
    # the leniency and closes the hole.
    from mcp.server.mcpserver.exceptions import ToolError  # optional dep

    declared = {
        name: set((getattr(tool, "parameters", {}) or {}).get("properties", {}))
        for name, tool in (tools or {}).items()
    }
    inner = server.call_tool

    async def call_tool_checked(name: str, arguments: dict, *args, **kwargs):
        unknown = sorted(set(arguments or {}) - declared.get(name, set()))
        if unknown:
            known = ", ".join(sorted(declared.get(name, set()))) or "none"
            raise ToolError(
                f"unknown argument(s) for {name}: {', '.join(unknown)}. This tool takes: {known}."
            )
        return await inner(name, arguments, *args, **kwargs)

    server.call_tool = call_tool_checked


async def _report(ctx, done: int, total: int | None) -> None:
    import contextlib

    # total is None when cases stream from a source with no limit — the run
    # size is not knowable in advance, so progress is indeterminate.
    message = f"{done}/{total} cells" if total is not None else f"{done} cells"
    with contextlib.suppress(Exception):
        await ctx.report_progress(done, total, message)


def serve(output_dir: str | None = None, config_path: str | None = None) -> None:
    """Run the server on stdio (blocks)."""
    build_server(output_dir, config_path).run("stdio")
