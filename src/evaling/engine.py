"""The run engine: execute the variants × models × cases matrix.

Failures are per-cell: a failing render or model call records an error on that
cell and the run continues. Results append to storage as they complete, so an
interrupted run can be resumed — cells with a record are skipped.
"""

import asyncio
import contextlib
import hashlib
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from evaling.cache import ResponseCache
from evaling.concurrency import bounded_gather
from evaling.config.cases import load_cases
from evaling.config.errors import ConfigError
from evaling.config.loader import resolve_prompt
from evaling.config.schema import (
    Case,
    CaseFileRef,
    EvalConfig,
    ModelSpec,
    Settings,
    VariantSpec,
)
from evaling.config.settings import resolve_settings
from evaling.content import MediaRef
from evaling.errors import EvalingError
from evaling.limits import limiter_for
from evaling.providers import Completion, CompletionRequest, create_provider
from evaling.providers.retry import call_with_retries
from evaling.render import render_messages
from evaling.scorers import create_scorers
from evaling.scoring import GateResult, aggregate, evaluate_gate
from evaling.secrets import build_env
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
    #: Non-fatal notices worth showing the user (e.g. an unenforceable cost cap).
    warnings: list[str] = field(default_factory=list)


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
    # ALL configured models get providers (judges need theirs); only selected
    # models get matrix cells. One secret env for the whole run: real
    # environment first, then any secrets file next to the config.
    secret_env, secret_warnings = build_env(config.base_dir)
    providers = {model.id: create_provider(model, secret_env) for model in config.models}
    try:
        return await _run_eval_impl(
            config,
            settings,
            providers,
            label=label,
            resume_run_id=resume_run_id,
            baseline_run_id=baseline_run_id,
            model_filter=model_filter,
            variant_filter=variant_filter,
            case_filter=case_filter,
            max_cost_usd=max_cost_usd,
            on_result=on_result,
            extra_warnings=secret_warnings,
        )
    finally:
        await asyncio.gather(
            *(provider.aclose() for provider in providers.values()), return_exceptions=True
        )


async def _run_eval_impl(
    config: EvalConfig,
    settings: Settings | None,
    providers: dict[str, Any],
    *,
    label: str | None,
    resume_run_id: str | None,
    baseline_run_id: str | None,
    model_filter: list[str] | None,
    variant_filter: list[str] | None,
    case_filter: list[str] | None,
    max_cost_usd: float | None,
    on_result: Callable[[ResultRecord], None] | None,
    extra_warnings: list[str] | None = None,
) -> RunResult:
    if settings is None:
        settings = resolve_settings(None, config.settings)

    variants_sel, models_sel, cases = select_matrix(
        config, models=model_filter, variants=variant_filter, cases=case_filter
    )
    prompts = {
        variant.name: resolve_prompt(variant.prompt, config.base_dir) for variant in variants_sel
    }
    _validate_media_support(variants_sel, models_sel, prompts)
    scorecard = create_scorers(config, providers)
    cache = ResponseCache(settings.cache_dir) if settings.cache else None

    store = RunStore(settings.output_dir)
    # Resolve the baseline up front: a missing pinned baseline must fail
    # before any model call, not after the run completes.
    baseline_id = _resolve_baseline(store, config, baseline_run_id)
    fingerprint = config_fingerprint(config)
    if resume_run_id is not None:
        writer = store.open_run(resume_run_id)
        if writer.meta.get("status") == "complete":
            raise StorageError(f"run {resume_run_id!r} is already complete; nothing to resume")
        if fingerprint != writer.meta.get("config_sha256"):
            raise StorageError(
                f"config does not match run {resume_run_id!r} "
                "(a resumed run must use the exact config — including referenced prompt, "
                "case, and attachment files — it started with)"
            )
        prior_records = store.load_results(resume_run_id)
        done = {record.key for record in prior_records}
    else:
        writer = store.create_run(config, label=label, config_sha256=fingerprint)
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
    limiters = {model.id: limiter_for(model) for model in config.models}

    async def execute(variant_name: str, model: ModelSpec, case: Case) -> ResultRecord:
        record = ResultRecord(variant=variant_name, model=model.id, case_id=case.id or "")
        try:
            await _execute_cell(record, variant_name, model, case)
        except Exception as exc:  # noqa: BLE001 - per-cell isolation: no cell may kill the run
            record.error = _describe_error(exc)
        await _append(record)
        if on_result is not None:
            # A flaky progress callback must not abort an otherwise-healthy run.
            with contextlib.suppress(Exception):
                on_result(record)
        return record

    async def _execute_cell(
        record: ResultRecord, variant_name: str, model: ModelSpec, case: Case
    ) -> None:
        rendered = render_messages(prompts[variant_name], case, config.base_dir)
        record.messages = serialize_messages(rendered)
        # Archiving inputs is bookkeeping: a full disk must not cost the cell.
        with contextlib.suppress(OSError):
            for message in rendered:
                for part in message.parts:
                    if isinstance(part, MediaRef):
                        await asyncio.to_thread(writer.store_artifact, part)

        if cache is not None:
            key = cache.key_for(model, rendered)
            async with key_locks[key]:
                completion = await asyncio.to_thread(cache.get, key)
                record.cached = completion is not None
                if completion is None:
                    completion = await _timed_call(record, model, rendered)
                    # cache.put swallows its own I/O errors for the same reason.
                    await asyncio.to_thread(cache.put, key, completion)
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
        # Per-model limits first: don't hold a cost-budget slot while queued
        # behind this model's own rate limit.
        async with limiters[model.id]:
            return await _budgeted_call(record, model, rendered)

    async def _budgeted_call(record: ResultRecord, model: ModelSpec, rendered) -> Completion:
        await budget.acquire()
        completion = None
        try:
            request = CompletionRequest(model=model, messages=rendered)
            provider = providers[model.id]
            retry_kwargs = (
                {} if model.max_retries is None else {"max_attempts": model.max_retries + 1}
            )
            start = time.perf_counter()
            completion = await call_with_retries(lambda: provider.complete(request), **retry_kwargs)
            record.latency_ms = round((time.perf_counter() - start) * 1000, 3)
            return completion
        finally:
            await budget.release(completion.cost_usd if completion else None)

    async def _append(record: ResultRecord) -> None:
        # Off-thread: appending is the one synchronous write on every cell's
        # path, and blocking the loop here throttles real concurrency.
        async with lock:
            await asyncio.to_thread(writer.append_result, record)

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

    warnings: list[str] = list(extra_warnings or [])
    if max_cost_usd is not None and budget.unknown_cost_seen:
        warnings.append(
            "--max-cost could not be enforced for every call: some models "
            "reported no cost (no built-in pricing and no params.pricing). "
            "Set params.pricing to track their spend."
        )

    aggregates = aggregate(records)
    baseline_overall = _load_baseline_overall(store, baseline_id)
    gate = evaluate_gate(config.thresholds, aggregates["overall"], baseline_overall)
    writer.finalize(counts, totals, aggregates, asdict(gate) if gate else None, warnings)
    return RunResult(
        run_id=writer.run_id,
        path=writer.path,
        records=records,
        counts=counts,
        totals=totals,
        aggregates=aggregates,
        gate=gate,
        warnings=warnings,
    )


def _validate_media_support(
    variants_sel: list[VariantSpec],
    models_sel: list[ModelSpec],
    prompts: dict[str, list],
) -> None:
    """Fail at validation time when a prompt uses media a provider can't send.

    REQUIREMENTS 4.2: unsupported part types error at config-validation time
    where possible, not mid-run.
    """
    from evaling.providers import provider_class

    for variant in variants_sel:
        kinds: set[str] = set()
        for message in prompts[variant.name]:
            content = message.content
            if isinstance(content, str):
                continue
            for part in content:
                dumped = part.model_dump()
                if "text" not in dumped:
                    kinds.add(next(iter(dumped)))
        if not kinds:
            continue
        for model in models_sel:
            unsupported = kinds - provider_class(model.provider).SUPPORTED_MEDIA
            if unsupported:
                raise ConfigError(
                    f"variant {variant.name!r} uses {', '.join(sorted(unsupported))} content, "
                    f"which model {model.id!r} (provider {model.provider!r}) does not support"
                )


def _resolve_baseline(store: RunStore, config: EvalConfig, override: str | None) -> str | None:
    """Resolve the gating baseline to a run id, in core so every entry point
    (CLI, Python API, MCP) gets identical semantics.

    ``override`` (any run reference) wins; otherwise ``thresholds.baseline``
    applies, where ``"regression"`` means the pinned baseline.
    """
    ref = override or config.thresholds.baseline
    if not ref:
        return None
    if ref == "regression":
        ref = "baseline"
    return store.resolve_ref(ref)


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
        self._unknown_cost = False
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

    @property
    def unknown_cost_seen(self) -> bool:
        """True once a call completed without a resolvable cost."""
        return self._unknown_cost

    async def release(self, cost: float | None) -> None:
        if self.limit is None:
            return
        async with self._cond:
            self.in_flight -= 1
            # A completed call always makes the budget knowable, even when its
            # cost is unknown: an unpriced model contributes 0 to the
            # projection, so it must not throttle the run. (Requiring a
            # *priced* call here silently serialized every run against local or
            # unpriced models to concurrency 1.)
            self.any_landed = True
            if cost is None:
                self._unknown_cost = True
            else:
                self.spent += cost
                self.max_call_cost = max(self.max_call_cost, cost)
            self._cond.notify_all()


def _describe_error(exc: Exception) -> str:
    # EvalingErrors are already user-facing; anything else keeps its type for context.
    if isinstance(exc, EvalingError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def config_fingerprint(config: EvalConfig) -> str:
    """Hash of the config AND the content of every file it references.

    The resume guard compares this, so editing a referenced prompt file, case
    dataset, or attachment between run and resume is caught — the plain config
    snapshot only contains those files' paths.
    """
    snapshot, _ = snapshot_config(config)
    digest = hashlib.sha256(snapshot.encode())
    for path in _referenced_files(config):
        try:
            digest.update(hashlib.sha256(Path(path).read_bytes()).digest())
        except FileNotFoundError:
            # The file will fail loudly at render time; for fingerprinting,
            # its absence is itself part of the state.
            digest.update(b"<missing>")
        except OSError as exc:
            raise ConfigError(f"could not read referenced file {path}: {exc}") from exc
    return digest.hexdigest()


def _referenced_files(config: EvalConfig) -> list[str]:
    paths: set[str] = set()
    for variant in config.variants:
        if isinstance(variant.prompt, str):
            paths.add(str((config.base_dir / variant.prompt).resolve()))
        for message in resolve_prompt(variant.prompt, config.base_dir):
            paths.update(_literal_media_paths(message, config.base_dir))
    for judge in config.judges.values():
        if isinstance(judge.rubric, str):
            paths.add(str((config.base_dir / judge.rubric).resolve()))
    if isinstance(config.cases, CaseFileRef):
        paths.add(str((config.base_dir / config.cases.file).resolve()))
    for case in load_cases(config):
        paths.update(case.files.values())
    return sorted(paths)


def _literal_media_paths(message, base_dir: Path) -> set[str]:
    """Media parts with literal (non-templated) paths, resolved.

    Templated expressions like ``{{ files.photo }}`` resolve per case and are
    covered by the case-attachment hashes instead.
    """
    paths: set[str] = set()
    content = message.content
    if isinstance(content, str):
        return paths
    for part in content:
        dumped = part.model_dump()
        if "text" in dumped:
            continue
        [expr] = dumped.values()
        if "{{" not in expr:
            candidate = Path(expr)
            if not candidate.is_absolute():
                candidate = base_dir / candidate
            paths.add(str(candidate.resolve()))
    return paths


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
    _validate_media_support(variants_sel, models_sel, prompts)
    secret_env, _ = build_env(config.base_dir)
    providers = {model.id: create_provider(model, secret_env) for model in config.models}
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
