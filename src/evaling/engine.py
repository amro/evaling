"""The run engine: execute the variants × models × cases matrix.

Failures are per-cell: a failing render or model call records an error on that
cell and the run continues. Results append to storage as they complete, so an
interrupted run can be resumed — cells with a record are skipped.
"""

import asyncio
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path
from typing import Any

from evaling.cache import ResponseCache
from evaling.concurrency import bounded_gather
from evaling.config.cases import load_cases
from evaling.config.loader import resolve_prompt
from evaling.config.schema import Case, EvalConfig, ModelSpec, Settings
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
) -> RunResult:
    return asyncio.run(
        run_eval_async(
            config,
            settings,
            label=label,
            resume_run_id=resume_run_id,
            baseline_run_id=baseline_run_id,
        )
    )


async def run_eval_async(
    config: EvalConfig,
    settings: Settings | None = None,
    *,
    label: str | None = None,
    resume_run_id: str | None = None,
    baseline_run_id: str | None = None,
) -> RunResult:
    if settings is None:
        settings = resolve_settings(None, config.settings)

    cases = load_cases(config)
    prompts = {
        variant.name: resolve_prompt(variant.prompt, config.base_dir) for variant in config.variants
    }
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

    async def execute(variant_name: str, model: ModelSpec, case: Case) -> ResultRecord:
        record = ResultRecord(variant=variant_name, model=model.id, case_id=case.id or "")
        try:
            await _execute_cell(record, variant_name, model, case)
        except Exception as exc:  # noqa: BLE001 - per-cell isolation: no cell may kill the run
            record.error = _describe_error(exc)
        await _append(record)
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
        request = CompletionRequest(model=model, messages=rendered)
        provider = providers[model.id]
        start = time.perf_counter()
        completion = await call_with_retries(lambda: provider.complete(request))
        record.latency_ms = round((time.perf_counter() - start) * 1000, 3)
        return completion

    async def _append(record: ResultRecord) -> None:
        async with lock:
            writer.append_result(record)

    factories = [
        partial(execute, variant.name, model, case)
        for variant in config.variants
        for model in config.models
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


def _describe_error(exc: Exception) -> str:
    # EvalingErrors are already user-facing; anything else keeps its type for context.
    if isinstance(exc, EvalingError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"
