"""The run engine: execute the variants × models × cases matrix.

Failures are per-cell: a failing render or model call records an error on that
cell and the run continues. Results append to storage as they complete, so an
interrupted run can be resumed — cells with a record are skipped.
"""

import asyncio
import time
from dataclasses import dataclass
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
from evaling.providers import CompletionRequest, create_provider
from evaling.providers.retry import call_with_retries
from evaling.render import render_messages
from evaling.storage import ResultRecord, RunStore, serialize_messages


@dataclass
class RunResult:
    run_id: str
    path: Path
    records: list[ResultRecord]
    counts: dict[str, int]
    totals: dict[str, Any]


def run_eval(
    config: EvalConfig,
    settings: Settings | None = None,
    *,
    label: str | None = None,
    resume_run_id: str | None = None,
) -> RunResult:
    return asyncio.run(run_eval_async(config, settings, label=label, resume_run_id=resume_run_id))


async def run_eval_async(
    config: EvalConfig,
    settings: Settings | None = None,
    *,
    label: str | None = None,
    resume_run_id: str | None = None,
) -> RunResult:
    if settings is None:
        settings = resolve_settings(None, config.settings)

    cases = load_cases(config)
    prompts = {
        variant.name: resolve_prompt(variant.prompt, config.base_dir) for variant in config.variants
    }
    providers = {model.id: create_provider(model) for model in config.models}
    cache = ResponseCache(settings.cache_dir) if settings.cache else None

    store = RunStore(settings.output_dir)
    if resume_run_id is not None:
        writer = store.open_run(resume_run_id)
        prior_records = store.load_results(resume_run_id)
        done = {record.key for record in prior_records}
    else:
        writer = store.create_run(config, label=label)
        prior_records = []
        done = set()

    lock = asyncio.Lock()

    async def execute(variant_name: str, model: ModelSpec, case: Case) -> ResultRecord:
        record = ResultRecord(variant=variant_name, model=model.id, case_id=case.id or "")
        try:
            rendered = render_messages(prompts[variant_name], case, config.base_dir)
        except EvalingError as exc:
            record.error = str(exc)
            await _append(record)
            return record

        record.messages = serialize_messages(rendered)
        for message in rendered:
            for part in message.parts:
                if isinstance(part, MediaRef):
                    writer.store_artifact(part)

        completion = None
        if cache is not None:
            key = cache.key_for(model, rendered)
            completion = cache.get(key)
            record.cached = completion is not None

        if completion is None:
            request = CompletionRequest(model=model, messages=rendered)
            provider = providers[model.id]
            start = time.perf_counter()
            try:
                completion = await call_with_retries(lambda: provider.complete(request))
            except EvalingError as exc:
                record.error = str(exc)
                await _append(record)
                return record
            record.latency_ms = round((time.perf_counter() - start) * 1000, 3)
            if cache is not None:
                cache.put(key, completion)

        record.output = completion.text
        record.input_tokens = completion.input_tokens
        record.output_tokens = completion.output_tokens
        record.cost_usd = completion.cost_usd
        await _append(record)
        return record

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
    writer.finalize(counts, totals)
    return RunResult(
        run_id=writer.run_id, path=writer.path, records=records, counts=counts, totals=totals
    )


def _total(records: list[ResultRecord], attr: str) -> int | float:
    return sum(value for r in records if (value := getattr(r, attr)) is not None)
