"""The run engine: execute the variants × models × cases matrix.

Failures are per-cell: a failing render or model call records an error on that
cell and the run continues. Results append to storage as they complete, so an
interrupted run can be resumed — cells with a record are skipped.
"""

import asyncio
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path
from typing import Any

from evaling.cache import ResponseCache
from evaling.concurrency import bounded_gather
from evaling.config.cases import load_cases
from evaling.config.errors import ConfigError
from evaling.config.loader import resolve_prompt
from evaling.config.schema import Case, EvalConfig, ModelSpec, Settings, VariantSpec
from evaling.config.settings import resolve_settings
from evaling.content import MediaRef
from evaling.errors import EvalingError
from evaling.providers import Completion, CompletionRequest, create_provider
from evaling.providers.retry import call_with_retries
from evaling.render import render_messages
from evaling.scorers import create_scorers
from evaling.scoring import GateResult, aggregate, evaluate_gate
from evaling.storage import (
    ResultRecord,
    RunStore,
    StorageError,
    serialize_messages,
    snapshot_config,
)


@dataclass
class RunResult:
    run_id: str
    path: Path
    records: list[ResultRecord]
    counts: dict[str, int]
    totals: dict[str, Any]
    aggregates: dict[str, Any]
    gate: GateResult | None


def run_eval(
    config: EvalConfig,
    settings: Settings | None = None,
    *,
    label: str | None = None,
    resume_run_id: str | None = None,
    baseline_run_id: str | None = None,
    model_filter: list[str] | None = None,
    variant_filter: list[str] | None = None,
    case_filter: list[str] | None = None,
    max_cost_usd: float | None = None,
    on_result: Callable[[ResultRecord], None] | None = None,
) -> RunResult:
    return asyncio.run(
        run_eval_async(
            config,
            settings,
            label=label,
            resume_run_id=resume_run_id,
            baseline_run_id=baseline_run_id,
            model_filter=model_filter,
            variant_filter=variant_filter,
            case_filter=case_filter,
            max_cost_usd=max_cost_usd,
            on_result=on_result,
        )
    )


async def run_eval_async(
    config: EvalConfig,
    settings: Settings | None = None,
    *,
    label: str | None = None,
    resume_run_id: str | None = None,
    baseline_run_id: str | None = None,
    model_filter: list[str] | None = None,
    variant_filter: list[str] | None = None,
    case_filter: list[str] | None = None,
    max_cost_usd: float | None = None,
    on_result: Callable[[ResultRecord], None] | None = None,
) -> RunResult:
    if settings is None:
        settings = resolve_settings(None, config.settings)

    variants_sel, models_sel, cases = select_matrix(
        config, models=model_filter, variants=variant_filter, cases=case_filter
    )
    prompts = {
        variant.name: resolve_prompt(variant.prompt, config.base_dir) for variant in variants_sel
    }
    # ALL configured models get providers (judges need theirs); only selected
    # models get matrix cells.
    providers = {model.id: create_provider(model) for model in config.models}
    scorecard = create_scorers(config, providers)
    cache = ResponseCache(settings.cache_dir) if settings.cache else None

    store = RunStore(settings.output_dir)
    if resume_run_id is not None:
        writer = store.open_run(resume_run_id)
        if writer.meta.get("status") == "complete":
            raise StorageError(f"run {resume_run_id!r} is already complete; nothing to resume")
        _, config_sha256 = snapshot_config(config)
        if config_sha256 != writer.meta.get("config_sha256"):
            raise StorageError(
                f"config does not match run {resume_run_id!r} "
                "(a resumed run must use the exact config it started with)"
            )
        prior_records = store.load_results(resume_run_id)
        done = {record.key for record in prior_records}
    else:
        writer = store.create_run(config, label=label)
        prior_records = []
        done = set()

    lock = asyncio.Lock()
    # Per-cache-key locks single-flight identical concurrent requests: the
    # second waiter finds the first's response in the cache instead of paying
    # for a duplicate provider call.
    key_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
    budget = _CostBudget(
        max_cost_usd, spent=sum(r.cost_usd for r in prior_records if r.cost_usd) or 0.0
    )

    async def execute(variant_name: str, model: ModelSpec, case: Case) -> ResultRecord:
        record = ResultRecord(variant=variant_name, model=model.id, case_id=case.id or "")
        try:
            await _execute_cell(record, variant_name, model, case)
        except Exception as exc:  # noqa: BLE001 - per-cell isolation: no cell may kill the run
            record.error = _describe_error(exc)
        await _append(record)
        if on_result is not None:
            on_result(record)
        return record

    async def _execute_cell(
        record: ResultRecord, variant_name: str, model: ModelSpec, case: Case
    ) -> None:
        rendered = render_messages(prompts[variant_name], case, config.base_dir)
        record.messages = serialize_messages(rendered)
        for message in rendered:
            for part in message.parts:
                if isinstance(part, MediaRef):
                    writer.store_artifact(part)

        if cache is not None:
            key = cache.key_for(model, rendered)
            async with key_locks[key]:
                completion = cache.get(key)
                record.cached = completion is not None
                if completion is None:
                    completion = await _timed_call(record, model, rendered)
                    cache.put(key, completion)
        else:
            completion = await _timed_call(record, model, rendered)

        record.output = completion.text
        record.input_tokens = completion.input_tokens
        record.output_tokens = completion.output_tokens
        record.cost_usd = completion.cost_usd

        for criterion, scorer in scorecard:
            entry: dict[str, Any] = {"weight": criterion.weight}
            try:
                result = await scorer.score(record.output, case)
                entry.update(score=result.score, passed=result.passed)
                if result.detail is not None:
                    entry["detail"] = result.detail
            except Exception as exc:  # noqa: BLE001 - a broken scorer fails the criterion, not the run
                entry.update(score=0.0, passed=False, error=_describe_error(exc))
            record.scores[criterion.criterion] = entry

    async def _timed_call(record: ResultRecord, model: ModelSpec, rendered) -> Completion:
        await budget.acquire()
        completion = None
        try:
            request = CompletionRequest(model=model, messages=rendered)
            provider = providers[model.id]
            start = time.perf_counter()
            completion = await call_with_retries(lambda: provider.complete(request))
            record.latency_ms = round((time.perf_counter() - start) * 1000, 3)
            return completion
        finally:
            await budget.release(completion.cost_usd if completion else None)

    async def _append(record: ResultRecord) -> None:
        async with lock:
            writer.append_result(record)

    factories = [
        partial(execute, variant.name, model, case)
        for variant in variants_sel
        for model in models_sel
        for case in cases
        if (variant.name, model.id, case.id) not in done
    ]
    new_records = await bounded_gather(factories, settings.concurrency)

    records = prior_records + new_records
    counts = {
        "total": len(records),
        "succeeded": sum(1 for r in records if r.error is None),
        "failed": sum(1 for r in records if r.error is not None),
        "cached": sum(1 for r in records if r.cached),
    }
    totals = {
        "input_tokens": _total(records, "input_tokens"),
        "output_tokens": _total(records, "output_tokens"),
        "cost_usd": _total(records, "cost_usd"),
    }

    aggregates = aggregate(records)
    baseline_overall = _load_baseline_overall(
        store, baseline_run_id or _baseline_from_thresholds(config)
    )
    gate = evaluate_gate(config.thresholds, aggregates["overall"], baseline_overall)
    writer.finalize(counts, totals, aggregates, asdict(gate) if gate else None)
    return RunResult(
        run_id=writer.run_id,
        path=writer.path,
        records=records,
        counts=counts,
        totals=totals,
        aggregates=aggregates,
        gate=gate,
    )


def _baseline_from_thresholds(config: EvalConfig) -> str | None:
    # "regression" means "the pinned baseline" — resolved by the CLI layer,
    # which passes an explicit baseline_run_id. A literal run id works here.
    baseline = config.thresholds.baseline
    return baseline if baseline and baseline != "regression" else None


def _load_baseline_overall(store: RunStore, run_id: str | None) -> dict[str, Any] | None:
    if run_id is None:
        return None
    meta = store.load_meta(run_id)
    aggregates = meta.get("aggregates")
    if not aggregates:
        raise StorageError(f"baseline run {run_id!r} has no aggregates (did it finish?)")
    return aggregates["overall"]


def _total(records: list[ResultRecord], attr: str) -> int | float:
    return sum(value for r in records if (value := getattr(r, attr)) is not None)


class _CostBudget:
    """Admission control for --max-cost under concurrency.

    Naive check-then-call lets every in-flight coroutine pass the check before
    any cost lands, overshooting by concurrency × call cost. This bounds the
    overshoot: until the first call's cost is known, exactly one call may be
    in flight; afterwards a call is admitted only when spent + in-flight,
    projected at the costliest observed call, stays under the limit.
    """

    def __init__(self, limit: float | None, spent: float = 0.0):
        self.limit = limit
        self.spent = spent
        self.in_flight = 0
        self.max_call_cost = 0.0
        self.any_landed = False
        self._cond = asyncio.Condition()

    async def acquire(self) -> None:
        if self.limit is None:
            return
        async with self._cond:
            while True:
                if self.spent >= self.limit:
                    raise EvalingError(
                        f"skipped: max cost limit reached (${self.spent:.4f} spent, "
                        f"limit ${self.limit:.4f})"
                    )
                projected = self.spent + self.in_flight * self.max_call_cost
                if self.in_flight == 0 or (self.any_landed and projected < self.limit):
                    self.in_flight += 1
                    return
                await self._cond.wait()

    async def release(self, cost: float | None) -> None:
        if self.limit is None:
            return
        async with self._cond:
            self.in_flight -= 1
            if cost is not None:
                self.spent += cost
                self.max_call_cost = max(self.max_call_cost, cost)
                self.any_landed = True
            self._cond.notify_all()


def _describe_error(exc: Exception) -> str:
    # EvalingErrors are already user-facing; anything else keeps its type for context.
    if isinstance(exc, EvalingError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def select_matrix(
    config: EvalConfig,
    *,
    models: list[str] | None = None,
    variants: list[str] | None = None,
    cases: list[str] | None = None,
) -> tuple[list[VariantSpec], list[ModelSpec], list[Case]]:
    """Validate filters and return exactly what the matrix will execute.

    The single authority for matrix membership: the engine, dry runs, and the
    CLI's request count all use this, so they cannot disagree. Filtering
    models here does NOT remove judge providers — judges are not matrix
    members in the first place.
    """
    variant_specs = config.variants
    if variants:
        known = {v.name for v in variant_specs}
        unknown = sorted(set(variants) - known)
        if unknown:
            raise ConfigError(
                f"unknown variant(s): {', '.join(unknown)} (available: {', '.join(sorted(known))})"
            )
        wanted = set(variants)
        variant_specs = [v for v in variant_specs if v.name in wanted]

    model_specs = config.models
    if models:
        known = {m.id for m in model_specs}
        unknown = sorted(set(models) - known)
        if unknown:
            raise ConfigError(
                f"unknown model(s): {', '.join(unknown)} (available: {', '.join(sorted(known))})"
            )
        wanted = set(models)
        model_specs = [m for m in model_specs if m.id in wanted]

    case_list = load_cases(config)
    if cases:
        known = {case.id for case in case_list}
        unknown = sorted(set(cases) - known)
        if unknown:
            raise ConfigError(
                f"unknown case id(s): {', '.join(unknown)} (available: {', '.join(sorted(known))})"
            )
        wanted = set(cases)
        case_list = [case for case in case_list if case.id in wanted]

    return variant_specs, model_specs, case_list


@dataclass
class DryRunReport:
    """What a run would do: the matrix, per-cell render outcomes, no model calls."""

    requests: int
    cells: list[dict[str, Any]]  # {variant, model, case_id, error: str|None}

    @property
    def errors(self) -> list[dict[str, Any]]:
        return [cell for cell in self.cells if cell["error"] is not None]


def dry_run(
    config: EvalConfig,
    *,
    model_filter: list[str] | None = None,
    variant_filter: list[str] | None = None,
    case_filter: list[str] | None = None,
) -> DryRunReport:
    """Validate everything a run needs — prompts, cases, scorers, rendering —
    without calling any model."""
    variants_sel, models_sel, cases = select_matrix(
        config, models=model_filter, variants=variant_filter, cases=case_filter
    )
    prompts = {
        variant.name: resolve_prompt(variant.prompt, config.base_dir) for variant in variants_sel
    }
    providers = {model.id: create_provider(model) for model in config.models}
    create_scorers(config, providers)  # fail fast on bad scorer config

    cells = []
    for variant in variants_sel:
        for model in models_sel:
            for case in cases:
                error = None
                try:
                    render_messages(prompts[variant.name], case, config.base_dir)
                except Exception as exc:  # noqa: BLE001 - reported per cell, like the engine
                    error = _describe_error(exc)
                cells.append(
                    {
                        "variant": variant.name,
                        "model": model.id,
                        "case_id": case.id,
                        "error": error,
                    }
                )
    return DryRunReport(requests=len(cells), cells=cells)
