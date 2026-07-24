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
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn

from evaling import __version__
from evaling.cli import display
from evaling.cli.scaffold import scaffold_project
from evaling.config import load_config, resolve_settings
from evaling.config.loader import load_project_settings
from evaling.engine import dry_run as engine_dry_run
from evaling.engine import run_eval, select_matrix
from evaling.errors import EvalingError
from evaling.export import export_run
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
        else:
            # Commands that don't need the full eval (show, list, baseline …)
            # still honor the project's settings block, so every command
            # resolves the same output/cache directories as `run`.
            eval_settings = load_project_settings(self.config_path or "eval.yaml")
        return resolve_settings(cli, eval_settings)

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
            Console(stderr=True, highlight=False).print(f"[red]error:[/red] {exc}")
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
@pass_app
@cli_errors
def run(
    app,
    config_arg,
    models,
    variants,
    case_ids,
    dry,
    max_cost,
    yes,
    no_cache,
    resume_ref,
    baseline_ref,
    label,
    concurrency,
):
    """Run the eval matrix and print the summary."""
    config = load_config(config_arg or app.config_path or "eval.yaml")
    model_filter = list(models) or None
    variant_filter = list(variants) or None
    case_filter = list(case_ids) or None

    if dry:
        _do_dry_run(app, config, model_filter, variant_filter, case_filter)
        return

    settings = app.settings(config, concurrency=concurrency, cache=False if no_cache else None)
    store = RunStore(settings.output_dir)

    # The same selection the engine will execute — filters validate here,
    # before any progress display.
    variants_sel, models_sel, cases_sel = select_matrix(
        config, models=model_filter, variants=variant_filter, cases=case_filter
    )
    count = len(variants_sel) * len(models_sel) * len(cases_sel)
    app.say(
        f"Running [bold]{count}[/bold] requests "
        f"({len(variants_sel)} variants × {len(models_sel)} models × {len(cases_sel)} cases)"
    )
    if count >= CONFIRM_THRESHOLD and not yes and sys.stdin.isatty():
        click.confirm(f"That is {count} model calls — continue?", abort=True)

    # baseline resolution (including thresholds.baseline: regression) is core
    # logic — the engine handles it; any run reference passes through.
    resume_run_id = store.resolve_ref(resume_ref) if resume_ref else None

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

        def on_result(record):
            progress.advance(task)
            if app.verbose:
                status = (
                    f"[red]{record.error}[/red]" if record.error else display.snip(record.output)
                )
                progress.console.print(
                    f"  {record.variant} × {record.model} × {record.case_id}: {status}"
                )

    else:
        on_result = None
        progress = None

    filters = {
        "model_filter": model_filter,
        "variant_filter": variant_filter,
        "case_filter": case_filter,
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
    if app.json_output:
        app.echo_json(
            {
                "run_id": result.run_id,
                "path": str(result.path),
                "counts": result.counts,
                "totals": result.totals,
                "aggregates": result.aggregates,
                "gate": gate,
            }
        )
    else:
        app.say("")
        app.show(display.matrix_table(result.aggregates))
        _say_totals(app, result.counts, result.totals)
        if gate:
            for line in display.gate_lines(gate):
                app.show(line)
        app.say(f"run [bold]{result.run_id}[/bold] stored in {result.path}")
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


def _do_dry_run(app, config, model_filter, variant_filter, case_filter):
    report = engine_dry_run(
        config,
        model_filter=model_filter,
        variant_filter=variant_filter,
        case_filter=case_filter,
    )
    if app.json_output:
        app.echo_json({"requests": report.requests, "cells": report.cells})
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
@pass_app
@cli_errors
def compare(app, ref_a, ref_b):
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
@click.option("--format", "fmt", type=click.Choice(["json", "csv", "md"]), required=True)
@click.option("--out", type=click.Path(), default=None, help="Write to a file instead of stdout.")
@pass_app
@cli_errors
def export(app, ref, fmt, out):
    """Render a stored run as json, csv, or markdown."""
    store = app.store()
    run_id = store.resolve_ref(ref)
    text = export_run(store.load_meta(run_id), store.load_results(run_id), fmt)
    if out:
        Path(out).write_text(text)
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
@click.option("--force", is_flag=True, help="Overwrite existing scaffold files.")
@pass_app
@cli_errors
def init(app, force):
    """Scaffold a working example eval (runs offline with the mock provider)."""
    created = scaffold_project(Path.cwd(), force=force)
    for path in created:
        app.console.print(f"created [bold]{path}[/bold]")
    app.say("\ntry it:  [bold]evaling run[/bold]")
