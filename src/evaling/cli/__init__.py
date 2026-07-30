"""Command-line interface: a thin wrapper over the evaling core library."""

import contextlib
import functools
import json as jsonlib
import signal
import sys
from dataclasses import asdict
from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape as markup_escape
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn

from evaling import __version__
from evaling.cli import display
from evaling.cli.scaffold import scaffold_project
from evaling.config import load_config, resolve_settings
from evaling.config.loader import load_project_settings
from evaling.config.schema import CaseSourceRef
from evaling.engine import dry_run as engine_dry_run
from evaling.engine import run_eval, select_matrix, select_variants_models
from evaling.errors import EvalingError
from evaling.export import export_run
from evaling.report import render_compare_html, render_run_html
from evaling.scoring import compare_aggregates
from evaling.storage import RunStore

CONFIRM_THRESHOLD = 100  # request count above which `run` asks before spending


class App:
    """Global CLI state from group-level flags."""

    def __init__(self, config_path, output_dir, cache_dir, no_color, quiet, verbose, json_output):
        self.config_path = config_path
        self.quiet = quiet
        self.verbose = verbose
        self.json_output = json_output
        self._cli_settings = {"output_dir": output_dir, "cache_dir": cache_dir}
        no_color_flag = True if no_color else None  # None lets rich honor NO_COLOR itself
        self.console = Console(no_color=no_color_flag, highlight=False)
        self.err = Console(stderr=True, no_color=no_color_flag, highlight=False)

    def settings(self, config=None, *, concurrency=None, cache=None):
        cli = dict(self._cli_settings)
        if concurrency is not None:
            cli["concurrency"] = concurrency
        if cache is not None:
            cli["cache"] = cache
        if config is not None:
            eval_settings = config.settings
            base_dir = config.base_dir
        else:
            # Commands that don't need the full eval (show, list, baseline …)
            # still honor the project's settings block, so every command
            # resolves the same output/cache directories as `run`.
            target = Path(self.config_path or "eval.yaml")
            eval_settings = load_project_settings(target)
            # No config file means no project to anchor to, so relative
            # directories stay relative to where the command was run.
            base_dir = target.resolve().parent if target.is_file() else None
        return resolve_settings(cli, eval_settings, base_dir=base_dir)

    def store(self, settings=None) -> RunStore:
        return RunStore((settings or self.settings()).output_dir)

    def echo_json(self, payload) -> None:
        click.echo(jsonlib.dumps(payload, indent=2, sort_keys=True, default=str))

    def say(self, message) -> None:
        if not self.quiet and not self.json_output:
            self.console.print(message)

    def show(self, renderable) -> None:
        """Print primary result output — suppressed by --quiet/--json."""
        if not self.quiet and not self.json_output:
            self.console.print(renderable)


pass_app = click.make_pass_decorator(App)


def cli_errors(fn):
    """Turn EvalingErrors into a clean message on stderr and exit code 2."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (EvalingError, OSError) as exc:
            # Escape the message: brackets in it (a JSON path, "evaling[mcp]",
            # a model's own output) would otherwise be eaten as rich markup or
            # raise MarkupError on the way out.
            Console(stderr=True, highlight=False).print(
                f"[red]error:[/red] {markup_escape(str(exc))}"
            )
            raise SystemExit(2) from exc

    return wrapper


@click.group(no_args_is_help=True)
@click.version_option(version=__version__, prog_name="evaling")
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(),
    default=None,
    help="Eval config file (default: eval.yaml).",
)
@click.option(
    "-o",
    "--output-dir",
    type=click.Path(),
    default=None,
    help="Where runs are stored (overrides settings layers).",
)
@click.option(
    "--cache-dir",
    type=click.Path(),
    default=None,
    help="Response cache location (overrides settings layers).",
)
@click.option("--no-color", is_flag=True, help="Disable colored output.")
@click.option("-q", "--quiet", is_flag=True, help="Only errors and essential output.")
@click.option("-v", "--verbose", is_flag=True, help="Extra per-cell detail.")
@click.option("--json", "json_output", is_flag=True, help="Machine-readable JSON on stdout.")
@click.pass_context
def main(ctx, config_path, output_dir, cache_dir, no_color, quiet, verbose, json_output):
    """Compare prompt variants and models, easily."""
    # `evaling list | head` should end quietly, not raise BrokenPipeError
    with contextlib.suppress(AttributeError, ValueError):  # Windows / non-main thread
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    ctx.obj = App(config_path, output_dir, cache_dir, no_color, quiet, verbose, json_output)


@main.command()
@click.argument("config_arg", required=False, type=click.Path())
@click.option("--model", "models", multiple=True, help="Only these models (repeatable).")
@click.option("--variant", "variants", multiple=True, help="Only these variants (repeatable).")
@click.option("--case", "case_ids", multiple=True, help="Only these case ids (repeatable).")
@click.option(
    "--sample",
    type=int,
    default=None,
    metavar="N",
    help="Evaluate a random N of the selected cases.",
)
@click.option(
    "--sample-seed",
    type=int,
    default=None,
    help="Seed for --sample, to repeat an earlier draw.",
)
@click.option(
    "--dry-run",
    "dry",
    is_flag=True,
    help="Validate config and render all prompts without calling any model.",
)
@click.option(
    "--max-cost",
    type=float,
    default=None,
    help="Stop issuing model calls once accumulated cost reaches this many USD.",
)
@click.option("-y", "--yes", is_flag=True, help="Skip the large-matrix confirmation.")
@click.option(
    "--no-look",
    is_flag=True,
    help="Never store or display prompts, outputs, or attachments — scores only.",
)
@click.option("--no-cache", is_flag=True, help="Bypass the response cache.")
@click.option("--resume", "resume_ref", default=None, help="Resume an interrupted run.")
@click.option(
    "--baseline",
    "baseline_ref",
    default=None,
    help="Gate against this run (id, label, 'latest', or 'baseline').",
)
@click.option("--label", default=None, help="Human-friendly name for this run.")
@click.option("--concurrency", type=int, default=None, help="Max parallel model calls.")
@click.option(
    "--html",
    "html_path",
    type=click.Path(),
    default=None,
    help="Write a self-contained HTML report here when the run finishes.",
)
@pass_app
@cli_errors
def run(
    app,
    config_arg,
    models,
    variants,
    case_ids,
    sample,
    sample_seed,
    dry,
    max_cost,
    no_look,
    yes,
    no_cache,
    resume_ref,
    baseline_ref,
    label,
    concurrency,
    html_path,
):
    """Run the eval matrix and print the summary."""
    config = load_config(_config_target(config_arg, app))
    model_filter = list(models) or None
    variant_filter = list(variants) or None
    case_filter = list(case_ids) or None

    _check_sample(sample, sample_seed)

    if dry:
        _do_dry_run(app, config, model_filter, variant_filter, case_filter, sample, sample_seed)
        return

    if no_look:
        # One-way: the flag can enable no-look but never disable what the
        # config asked for.
        config = config.model_copy(
            update={"privacy": config.privacy.model_copy(update={"no_look": True})}
        )
    settings = app.settings(config, concurrency=concurrency, cache=False if no_cache else None)
    store = RunStore(settings.output_dir)

    if isinstance(config.cases, CaseSourceRef):
        # A source is walked lazily, so there is no count to show or confirm.
        # An unbounded one with no ceiling is a bill, not a run.
        if config.cases.limit is None and max_cost is None and not yes:
            raise click.UsageError(
                "this config fetches cases from a source with no `limit`, so evaling "
                "cannot tell how many model calls the run will make. Set `limit` in the "
                "config, pass --max-cost, or pass --yes to run it anyway."
            )
        variants_sel, models_sel = select_variants_models(
            config, models=model_filter, variants=variant_filter
        )
        bound = f"up to {config.cases.limit} cases" if config.cases.limit else "all cases"
        app.say(
            f"Running {len(variants_sel)} variants × {len(models_sel)} models over "
            f"[bold]{bound}[/bold] from the case source"
        )
        _say_judge_only(app, config)
        # `limit` gives a real total; without one the source size is unknown
        # until it is walked, so the progress bar runs indeterminate.
        count = (
            len(variants_sel) * len(models_sel) * config.cases.limit if config.cases.limit else None
        )
    else:
        # The same selection the engine will execute — filters validate here,
        # before any progress display.
        variants_sel, models_sel, cases_sel = select_matrix(
            config, models=model_filter, variants=variant_filter, cases=case_filter
        )
        # The draw itself happens in the engine, which records its seed; here
        # only the size matters, and that is the same for any draw.
        available = len(cases_sel)
        selected = min(sample, available) if sample is not None else available
        count = len(variants_sel) * len(models_sel) * selected
        app.say(
            f"Running [bold]{count}[/bold] requests "
            f"({len(variants_sel)} variants × {len(models_sel)} models × {selected} cases)"
        )
        if sample is not None:
            app.say(f"  sampling {selected} of {available} cases")
        _say_judge_only(app, config)
        if count >= CONFIRM_THRESHOLD and not yes and sys.stdin.isatty():
            click.confirm(f"That is {count} model calls — continue?", abort=True)

    # baseline resolution (including thresholds.baseline: regression) is core
    # logic — the engine handles it; any run reference passes through.
    # `is not None`, not truthiness: `--resume ""` — an unset shell variable in
    # a script — used to fall through to a fresh run, which in CI is a silent
    # second bill rather than an error.
    resume_run_id = store.resolve_ref(resume_ref) if resume_ref is not None else None

    show_progress = not (app.quiet or app.json_output)
    if show_progress:
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            console=app.console,
        )
        task = progress.add_task("evaluating", total=count)
        live = {"cost": 0.0, "failed": 0}

        def on_result(record):
            live["cost"] += record.cost_usd or 0.0
            live["failed"] += 1 if record.error else 0
            label_bits = []
            if live["cost"]:
                label_bits.append(f"${live['cost']:.4f}")
            if live["failed"]:
                label_bits.append(f"{live['failed']} failed")
            progress.update(
                task,
                advance=1,
                description="evaluating" + (f" · {' · '.join(label_bits)}" if label_bits else ""),
            )
            if app.verbose:
                status = (
                    f"[red]{display.snip(record.error)}[/red]"
                    if record.error
                    else display.snip(record.output)
                )
                progress.console.print(
                    f"  {display.safe(record.variant)} × {display.safe(record.model)} × "
                    f"{display.safe(record.case_id)}: {status}"
                )

    else:
        on_result = None
        progress = None

    filters = {
        "model_filter": model_filter,
        "variant_filter": variant_filter,
        "case_filter": case_filter,
        "sample": sample,
        "sample_seed": sample_seed,
    }
    if progress is not None:
        with progress:
            result = _execute(
                config,
                settings,
                label,
                resume_run_id,
                baseline_ref,
                filters,
                max_cost,
                on_result,
            )
    else:
        result = _execute(
            config, settings, label, resume_run_id, baseline_ref, filters, max_cost, None
        )

    gate = asdict(result.gate) if result.gate else None
    if html_path is not None:
        _require_path(html_path, "--html")
        # Read the run back from storage so the report is rendered from the
        # same source of truth as `evaling export`.
        meta = store.load_meta(result.run_id)
        Path(html_path).write_text(
            render_run_html(meta, store.load_results(result.run_id)), encoding="utf-8", newline="\n"
        )

    if app.json_output:
        app.echo_json(
            {
                "run_id": result.run_id,
                "path": str(result.path),
                "counts": result.counts,
                "totals": result.totals,
                "aggregates": result.aggregates,
                "gate": gate,
                "warnings": result.warnings,
                "selection": result.selection,
                **({"html": str(html_path)} if html_path else {}),
            }
        )
    else:
        app.say("")
        for warning in result.warnings:
            app.err.print(f"[yellow]warning:[/yellow] {markup_escape(warning)}")
        app.show(display.matrix_table(result.aggregates))
        _say_totals(app, result.counts, result.totals)
        if gate:
            for line in display.gate_lines(gate):
                app.show(line)
        if result.selection:
            app.say(
                f"sampled {result.selection['sample']} of "
                f"{result.selection['available']} cases — repeat this draw with "
                f"[bold]--sample {result.selection['sample']} "
                f"--sample-seed {result.selection['seed']}[/bold]"
            )
        app.say(f"run [bold]{result.run_id}[/bold] stored in {result.path}")
        if html_path:
            app.say(f"report written to [bold]{html_path}[/bold]")
    if gate and not gate["passed"]:
        if app.quiet and not app.json_output:
            app.err.print("[red]gate FAILED[/red]")
        raise SystemExit(1)


def _execute(config, settings, label, resume_run_id, baseline_ref, filters, max_cost, on_result):
    return run_eval(
        config,
        settings,
        label=label,
        resume_run_id=resume_run_id,
        baseline_run_id=baseline_ref,
        max_cost_usd=max_cost,
        on_result=on_result,
        **filters,
    )


def _say_totals(app, counts, totals):
    cost = totals.get("cost_usd") or 0.0
    app.say(
        f"{counts['succeeded']}/{counts['total']} succeeded, "
        f"{counts['failed']} failed, {counts['cached']} cached — "
        f"{totals['input_tokens']} in / {totals['output_tokens']} out tokens, ${cost:.4f}"
    )


def _require_path(value: str, flag: str) -> None:
    """An empty path silently skipped the write it was asked for."""
    if not str(value).strip():
        raise click.UsageError(f"{flag} was given an empty path")


def _config_target(config_arg: str | None, app) -> str:
    """Which config to load, refusing an empty one.

    `evaling run ""` — an unset variable in a script — used to fall through to
    the default config, so the run silently evaluated something other than what
    was asked for.
    """
    for candidate in (config_arg, app.config_path):
        if candidate is not None:
            if not str(candidate).strip():
                raise click.UsageError("config path is empty")
            return candidate
    return "eval.yaml"


def _say_judge_only(app, config) -> None:
    """Name models that are judging rather than being evaluated.

    The defect this replaced was not the default but its invisibility: a judge's
    model silently became a candidate and nothing in the output said so.
    """
    judges = [m.id for m in config.models if m.role == "judge"]
    if judges:
        app.say(f"  {display.safe(', '.join(judges))}: judge only, not evaluated")


def _check_sample(sample, sample_seed) -> None:
    if sample is not None and sample < 1:
        raise click.UsageError("--sample must be at least 1")
    if sample_seed is not None and sample is None:
        # Silently doing nothing here would look like a draw was pinned.
        raise click.UsageError("--sample-seed has no effect without --sample")


def _do_dry_run(
    app, config, model_filter, variant_filter, case_filter, sample=None, sample_seed=None
):
    report = engine_dry_run(
        config,
        model_filter=model_filter,
        variant_filter=variant_filter,
        case_filter=case_filter,
        sample=sample,
        sample_seed=sample_seed,
    )
    if app.json_output:
        app.echo_json(
            {
                "requests": report.requests,
                "cells": report.cells,
                "sampled": report.sampled,
                "source_total": report.source_total,
            }
        )
    else:
        if report.sampled:
            app.console.print(
                f"Sampled [bold]{report.requests}[/bold] cells from the case source "
                "(its total is unknown, so this is a sample, not the run size); "
                "no model was called."
            )
        else:
            app.console.print(
                f"[bold]{report.requests}[/bold] requests would be made; no model was called."
            )
        for cell in report.errors:
            app.console.print(
                f"  [red]✗[/red] {cell['variant']} × {cell['model']} × {cell['case_id']}: "
                f"{cell['error']}"
            )
        if not report.errors:
            app.say("all prompts render cleanly")
    if report.errors:
        raise SystemExit(2)


@main.command()
@click.argument("ref")
@click.option("--failures", is_flag=True, help="List failing cells only.")
@click.option("--case", "case_id", default=None, help="Drill into one case, side by side.")
@pass_app
@cli_errors
def show(app, ref, failures, case_id):
    """Re-render a stored run (summary, failures, or one case)."""
    store = app.store()
    run_id = store.resolve_ref(ref)
    meta = store.load_meta(run_id)
    records = store.load_results(run_id)

    if app.json_output:
        payload = {"run": meta, "results": [asdict(record) for record in records]}
        if case_id:
            payload["results"] = [asdict(r) for r in records if r.case_id == case_id]
        app.echo_json(payload)
        return

    if case_id:
        subset = [record for record in records if record.case_id == case_id]
        if not subset:
            raise EvalingError(f"no results for case {case_id!r} in run {run_id}")
        app.console.print(f"[bold]case {case_id}[/bold] in run {run_id}")
        app.console.print(display.case_table(subset))
        if app.verbose:
            for record in subset:
                app.console.print(f"\n[bold]{record.variant} × {record.model}[/bold]")
                app.console.print(record.error or record.output or "")
        return

    if failures:
        lines = display.failure_lines(records)
        if not lines:
            app.console.print("[green]no failures[/green]")
        for line in lines:
            app.console.print(line)
        return

    label = f" ({meta['label']})" if meta.get("label") else ""
    app.console.print(f"[bold]{meta['id']}[/bold]{label} — {meta['status']}")
    if meta.get("aggregates"):
        app.console.print(display.matrix_table(meta["aggregates"]))
    if meta.get("counts"):
        _say_totals(app, meta["counts"], meta["totals"])
    if meta.get("gate"):
        for line in display.gate_lines(meta["gate"]):
            app.console.print(line)


@main.command(name="list")
@click.option("--limit", type=int, default=20, help="Show at most this many runs.")
@pass_app
@cli_errors
def list_runs(app, limit):
    """List stored runs, newest first."""
    runs = list(reversed(app.store().list_runs()))[:limit]
    if app.json_output:
        app.echo_json(runs)
        return
    if not runs:
        app.console.print("no runs yet")
        return
    app.console.print(display.runs_table(runs))


@main.command()
@click.argument("ref_a")
@click.argument("ref_b")
@click.option(
    "--html",
    "html_path",
    type=click.Path(),
    default=None,
    help="Write a self-contained HTML comparison here.",
)
@pass_app
@cli_errors
def compare(app, ref_a, ref_b, html_path):
    """Compare two runs: per-cell score and pass-rate deltas."""
    store = app.store()
    metas = []
    for ref in (ref_a, ref_b):
        meta = store.load_meta(store.resolve_ref(ref))
        if not meta.get("aggregates"):
            raise EvalingError(f"run {meta['id']} has no aggregates (did it finish?)")
        metas.append(meta)
    meta_a, meta_b = metas
    diff = compare_aggregates(meta_a["aggregates"], meta_b["aggregates"])
    if html_path is not None:
        _require_path(html_path, "--html")
        Path(html_path).write_text(
            render_compare_html(meta_a, meta_b, diff), encoding="utf-8", newline="\n"
        )

    if app.json_output:
        app.echo_json({"a": meta_a["id"], "b": meta_b["id"], **diff})
        return

    app.console.print(f"[bold]{meta_a['id']}[/bold] → [bold]{meta_b['id']}[/bold]")
    table, notes = display.compare_table(diff)
    app.console.print(table)
    for note in notes:
        app.console.print(f"[yellow]{note}[/yellow]")
    overall = diff["overall"]
    app.console.print(
        f"overall: score {display.score3(overall['score_a'])} → "
        f"{display.score3(overall['score_b'])}, "
        f"pass rate {display.pct(overall['pass_rate_a'])} → "
        f"{display.pct(overall['pass_rate_b'])}"
    )


@main.command()
@click.argument("ref")
@click.option("--format", "fmt", type=click.Choice(["json", "csv", "md", "html"]), required=True)
@click.option("--out", type=click.Path(), default=None, help="Write to a file instead of stdout.")
@pass_app
@cli_errors
def export(app, ref, fmt, out):
    """Render a stored run as json, csv, or markdown."""
    store = app.store()
    run_id = store.resolve_ref(ref)
    text = export_run(store.load_meta(run_id), store.load_results(run_id), fmt)
    if out is not None:
        _require_path(out, "--out")
        Path(out).write_text(text, encoding="utf-8", newline="\n")
        app.say(f"wrote {out}")
    else:
        click.echo(text)


@main.group()
def baseline():
    """Manage the pinned baseline run used for regression gating."""


@baseline.command(name="set")
@click.argument("ref")
@pass_app
@cli_errors
def baseline_set(app, ref):
    """Pin a run (id, label, or 'latest') as the baseline."""
    store = app.store()
    run_id = store.resolve_ref(ref)
    store.set_baseline(run_id)
    app.console.print(f"baseline pinned to [bold]{run_id}[/bold]")


@baseline.command(name="show")
@pass_app
@cli_errors
def baseline_show(app):
    """Print the pinned baseline run id."""
    pinned = app.store().get_baseline()
    if app.json_output:
        app.echo_json({"baseline": pinned})
    elif pinned:
        app.console.print(pinned)
    else:
        app.console.print("no baseline pinned")


@main.command()
@click.argument("config_arg", required=False, type=click.Path())
@click.option("--model", "models", multiple=True, help="Only these models (repeatable).")
@click.option("--variant", "variants", multiple=True, help="Only these variants (repeatable).")
@click.option("--case", "case_ids", multiple=True, help="Only these case ids (repeatable).")
@click.option(
    "--sample",
    type=int,
    default=None,
    metavar="N",
    help="Check a random N of the selected cases.",
)
@click.option(
    "--sample-seed",
    type=int,
    default=None,
    help="Seed for --sample, to repeat an earlier draw.",
)
@pass_app
@cli_errors
def validate(app, config_arg, models, variants, case_ids, sample, sample_seed):
    """Check the config and render every prompt without calling any model.

    The same work as `run --dry-run`, named so it's findable.
    """
    _check_sample(sample, sample_seed)
    config = load_config(_config_target(config_arg, app))
    _do_dry_run(
        app,
        config,
        list(models) or None,
        list(variants) or None,
        list(case_ids) or None,
        sample,
        sample_seed,
    )


@main.group()
def cache():
    """Inspect or clear the response cache."""


@cache.command(name="info")
@pass_app
@cli_errors
def cache_info(app):
    """Show where the cache lives, how many entries it holds, and its size."""
    from evaling.cache import ResponseCache

    stats = ResponseCache(app.settings().cache_dir).stats()
    if app.json_output:
        app.echo_json(stats)
        return
    megabytes = stats["bytes"] / 1_048_576
    app.console.print(f"{stats['entries']} entries · {megabytes:.1f} MB · {stats['path']}")


@cache.command(name="clear")
@click.option(
    "--older-than",
    type=float,
    default=None,
    metavar="DAYS",
    help="Only remove entries older than this many days.",
)
@click.option("-y", "--yes", is_flag=True, help="Skip the confirmation.")
@pass_app
@cli_errors
def cache_clear(app, older_than, yes):
    """Delete cached responses (all, or just the stale ones)."""
    from evaling.cache import ResponseCache

    store = ResponseCache(app.settings().cache_dir)
    stats = store.stats()
    if not stats["entries"]:
        app.console.print("cache is already empty")
        return
    scope = "everything" if older_than is None else f"entries older than {older_than} days"
    if not yes and sys.stdin.isatty():
        click.confirm(f"Delete {scope} from {stats['path']}?", abort=True)
    removed = store.prune(older_than)
    app.console.print(f"removed [bold]{removed}[/bold] cached response(s)")


@main.command()
@pass_app
@cli_errors
def mcp(app):
    """Start the MCP server on stdio (for agent-driven prompt iteration)."""
    from evaling.mcp_server import serve

    # Resolve the output dir the same way every other command does, so an
    # agent sees exactly the runs the CLI sees.
    serve(output_dir=str(app.settings().output_dir), config_path=app.config_path)


@main.command()
@click.option("--force", is_flag=True, help="Overwrite existing scaffold files.")
@click.option(
    "--provider",
    type=click.Choice(["mock", "anthropic", "openai", "openai-compatible"]),
    default="mock",
    help="Scaffold for this provider (default: mock, which runs offline).",
)
@pass_app
@cli_errors
def init(app, force, provider):
    """Scaffold a working example eval (offline by default, via the mock provider)."""
    created = scaffold_project(Path.cwd(), force=force, provider=provider)
    for path in created:
        app.console.print(f"created [bold]{path}[/bold]")
    app.say("\ntry it:  [bold]evaling run[/bold]")
