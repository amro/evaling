"""The run engine: execute the variants × models × cases matrix.

Failures are per-cell: a failing render or model call records an error on that
cell and the run continues. Results append to storage as they complete, so an
interrupted run can be resumed — cells with a record are skipped.
"""

import asyncio
import contextlib
import hashlib
import math
import random
import secrets
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from evaling.cache import ResponseCache
from evaling.concurrency import KeyedLocks, consume_bounded
from evaling.config.cases import load_cases
from evaling.config.errors import ConfigError
from evaling.config.loader import resolve_prompt
from evaling.config.schema import (
    Case,
    CaseFileRef,
    CaseSourceRef,
    EvalConfig,
    ModelSpec,
    Privacy,
    Settings,
    VariantSpec,
)
from evaling.config.settings import resolve_settings
from evaling.content import MediaRef
from evaling.errors import EvalingError
from evaling.limits import limiter_for
from evaling.privacy import hash_case_id, redact_record, scrub_secrets
from evaling.providers import Completion, CompletionRequest, create_provider
from evaling.providers.pricing import ASSUMED_OUTPUT_TOKENS, CostEstimate, estimate_run
from evaling.providers.retry import call_with_retries
from evaling.render import render_messages
from evaling.reqlog import open_log
from evaling.scorers import create_scorers
from evaling.scoring import Aggregator, GateResult, cell_summary, evaluate_gate
from evaling.secrets import build_env, redact
from evaling.sources import (
    close_source,
    iter_source_cases,
    load_source,
    source_count,
    source_errors,
)
from evaling.storage import (
    ResultRecord,
    RunStore,
    StorageError,
    serialize_messages,
    snapshot_config,
)

#: Above this many cells a run stops handing every record back in memory.
#: Counts, totals, and aggregates are unaffected — they are accumulated as the
#: run proceeds — but ``records`` would be hundreds of megabytes of prompts and
#: outputs that the caller has already got on disk.
MAX_RETAINED_RECORDS = 10_000


@dataclass
class RunResult:
    run_id: str
    path: Path
    #: Every record, unless the run exceeded :data:`MAX_RETAINED_RECORDS`, in
    #: which case this is empty and ``records_truncated`` is True. Empty rather
    #: than partial on purpose: a partial list would silently give a caller
    #: wrong answers, while an empty one sends them to :meth:`iter_records`.
    records: list[ResultRecord]
    counts: dict[str, int]
    totals: dict[str, Any]
    aggregates: dict[str, Any]
    gate: GateResult | None
    #: Non-fatal notices worth showing the user (e.g. an unenforceable cost cap).
    warnings: list[str] = field(default_factory=list)
    #: True when the run was too large to keep in memory; use iter_records().
    records_truncated: bool = False
    #: True when --fail-fast ended the run at the first failing cell, so the
    #: matrix is deliberately incomplete rather than merely unfinished.
    stopped_early: bool = False
    #: True when the cost ceiling ended the run with cells still owed. Unlike
    #: ``stopped_early`` this leaves the run resumable, because those cells
    #: were never attempted rather than attempted and failed.
    incomplete: bool = False
    #: How the cases were narrowed, when they were: ``{sample, seed, available}``.
    #: The seed is what makes a sampled run repeatable, so it is reported back
    #: as well as stored — a draw nobody can reproduce is not much of a draw.
    selection: dict[str, Any] | None = None

    def iter_records(self) -> Iterator[ResultRecord]:
        """Stream every record from disk, regardless of run size."""
        return RunStore(self.path.parent).iter_results(self.run_id)


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
    sample: int | None = None,
    sample_seed: int | None = None,
    max_cost_usd: float | None = None,
    fail_fast: bool = False,
    log_requests: str | Path | None = None,
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
            sample=sample,
            sample_seed=sample_seed,
            max_cost_usd=max_cost_usd,
            fail_fast=fail_fast,
            log_requests=log_requests,
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
    sample: int | None = None,
    sample_seed: int | None = None,
    max_cost_usd: float | None = None,
    fail_fast: bool = False,
    log_requests: str | Path | None = None,
    on_result: Callable[[ResultRecord], None] | None = None,
) -> RunResult:
    # ALL configured models get providers (judges need theirs); only selected
    # models get matrix cells. One secret env for the whole run: real
    # environment first, then any secrets file next to the config.
    secret_env, secret_warnings = build_env(config.base_dir)
    request_log = open_log(log_requests, secret_env, no_look=config.privacy.no_look)
    providers = {
        model.id: create_provider(model, secret_env, config.base_dir, request_log)
        for model in config.models
    }
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
            sample=sample,
            sample_seed=sample_seed,
            max_cost_usd=max_cost_usd,
            fail_fast=fail_fast,
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
    sample: int | None,
    sample_seed: int | None,
    max_cost_usd: float | None,
    fail_fast: bool,
    on_result: Callable[[ResultRecord], None] | None,
    extra_warnings: list[str] | None = None,
) -> RunResult:
    if settings is None:
        settings = resolve_settings(None, config.settings, base_dir=config.base_dir)

    privacy = config.privacy
    secret_values = _known_credentials(providers)
    source_ref = config.cases if isinstance(config.cases, CaseSourceRef) else None
    store = RunStore(settings.output_dir)

    # Resolved before the cases are chosen, because a resumed run has to draw
    # the same sample the original did — otherwise its two halves cover
    # different cases while looking like one run.
    sample, sample_seed = _resolve_sample(store, resume_run_id, sample, sample_seed)

    if source_ref is not None:
        variants_sel, models_sel = select_variants_models(
            config, models=model_filter, variants=variant_filter
        )
        cases: list[Case] = []
        if case_filter:
            raise ConfigError(
                "--case cannot filter a source-backed run: cases are fetched lazily, "
                "so evaling does not know the ids in advance. Filter inside your "
                "source, or use `limit` to take fewer."
            )
        if sample is not None:
            raise ConfigError(
                "sampling cannot narrow a source-backed run: cases are fetched lazily, "
                "so there is no population to draw from. Use `limit` in the config to "
                "take fewer, or sample inside your source."
            )
        available = 0
    else:
        variants_sel, models_sel, cases = select_matrix(
            config, models=model_filter, variants=variant_filter, cases=case_filter
        )
        population = [case.id or "" for case in cases]
        available = len(cases)
        cases = sample_cases(cases, sample, sample_seed)
    selection = (
        None
        if sample is None
        else {"sample": len(cases), "seed": sample_seed, "available": available}
    )
    # What this run is actually going to execute. Recorded so a resume can
    # prove it is finishing the same run rather than a differently-filtered
    # one — the config fingerprint covers the config, not the flags.
    matrix = (
        None
        if source_ref is not None
        else {
            "variants": sorted(variant.name for variant in variants_sel),
            "models": sorted(model.id for model in models_sel),
            # Digests rather than the ids themselves: a run over 500,000 cases
            # would otherwise write every one of them into run.json. Identities
            # rather than counts, because swapping two cases for two others
            # leaves every count unchanged.
            "cases": _ids_digest(case.id or "" for case in cases),
            "population": _ids_digest(population),
            "count": len(cases),
        }
    )
    prompts = {
        variant.name: resolve_prompt(variant.prompt, config.base_dir) for variant in variants_sel
    }
    _validate_media_support(variants_sel, models_sel, prompts)

    # The response cache stores prompts and completions verbatim, which is
    # exactly what no-look exists to prevent.
    cache = ResponseCache(settings.cache_dir) if settings.cache and not privacy.no_look else None

    # Resolve the baseline up front: a missing pinned baseline must fail
    # before any model call, not after the run completes.
    # Fail on a bad scorecard before a run directory exists. Scorers are built
    # for real further down, once the limiters and budget a judge needs exist.
    create_scorers(config, providers)
    baseline_id = _resolve_baseline(store, config, baseline_run_id)
    # Read now, not after the run: the comment above says a bad baseline must
    # fail before any model call, and resolving the id alone did not check the
    # run had finished. A baseline pointing at an unfinished run used to fail
    # the run after full spend and leave it wedged in status "running".
    baseline_overall = _load_baseline_overall(store, baseline_id)
    fingerprint = config_fingerprint(config)
    if resume_run_id is not None and source_ref is not None:
        raise StorageError(
            "resume is not supported for source-backed runs. A source can return "
            "different rows on the second call — inserted, mutated, or aged out — "
            "and evaling cannot verify that it did not. The halves of such a run "
            "would describe different data while looking entirely normal. Start a "
            "fresh run (see REQUIREMENTS.md, M11, for the reasoning)."
        )
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
        _check_resumable_matrix(resume_run_id, writer.meta.get("matrix"), matrix)
        prior_records = store.load_results(resume_run_id)
        done = {record.key for record in prior_records}
        # Records are stored with whatever id no-look left on them, so the
        # candidate key has to be transformed the same way. Comparing raw ids
        # against hashed ones matched nothing, and a resumed no-look run
        # therefore re-ran and re-billed every cell it had already finished.
    else:
        writer = store.create_run(
            config,
            label=label,
            config_sha256=fingerprint,
            redact_cases=privacy.no_look,
            selection=selection,
            matrix=matrix,
        )
        prior_records = []
        done = set()

    lock = asyncio.Lock()
    # Single-flights identical concurrent requests: the second waiter finds the
    # first's response in the cache instead of paying for a duplicate call.
    single_flight = KeyedLocks()

    # Judge spend is real spend. It is not attributable to a cell — a judge is
    # not a matrix member — so it is tracked here and reported separately.
    # Seeded from what the run has already spent on judges. finalize()
    # overwrites the total rather than adding to it, so starting from zero
    # made a resumed run report only its second half's judge cost.
    prior_spend = _prior_spend(store, resume_run_id)
    prior_judge_cost = prior_spend["judge_cost_usd"]
    prior_unattributed = prior_spend["unattributed_cost_usd"]

    budget = _CostBudget(
        max_cost_usd,
        # Cached cells cost nothing, so seeding the budget with them would
        # make a resumed run believe it had already spent money it hadn't.
        #
        # Prior judge spend belongs here too. --max-cost is a ceiling on the
        # run, and a resume continues that run — leaving judges out let every
        # resume spend the ceiling afresh, so a heavily judged eval stopped
        # and resumed three times cost three ceilings.
        # Every dollar the earlier segments spent: recorded cells, judges
        # (which are not cells and leave no record), and cells that paid for a
        # candidate call and were then dropped when a judge hit the ceiling.
        # Leave any of them out and each resume spends the ceiling afresh.
        spent=(sum(r.cost_usd for r in prior_records if r.cost_usd and not r.cached) or 0.0)
        + prior_judge_cost
        + prior_unattributed,
    )
    limiters = {model.id: limiter_for(model) for model in config.models}

    judge_spend = [prior_judge_cost]
    #: Money already paid by cells whose record was deliberately dropped —
    #: the candidate call completed and billed, then the cost ceiling refused
    #: the judge. Without this the spend disappears from every ledger: the run
    #: reports less than it cost, and each resume pays it again.
    unattributed_spend = [prior_unattributed]
    # Counted alongside the spend, and cumulative for the same reason: both
    # describe the run, which a resume continues rather than replaces.
    judge_calls = [int(prior_spend["judge_calls"])]

    async def _governed_call(model: ModelSpec, rendered) -> Completion:
        """A model call from outside the matrix — currently an LLM judge.

        Cached like any other call. A judge sees the model's output and the
        rubric, both of which the key covers, so an identical judgment has an
        identical answer — and without this a rerun that served every cell
        from disk still paid for every judgment, which made "the second run is
        free" true of exactly the runs that cost the least.
        """
        if cache is None:
            return await _billed_call(model, rendered)
        key = cache.key_for(model, rendered)
        async with single_flight(key):
            completion = await asyncio.to_thread(cache.get, key)
            if completion is None:
                completion = await _billed_call(model, rendered)
                await asyncio.to_thread(cache.put, key, completion)
            return completion

    async def _billed_call(model: ModelSpec, rendered) -> Completion:
        """The paid half of a judge call.

        A judge is a real, billable model call. Calling the provider directly
        would put it outside the cost budget and outside that model's own
        concurrency and rate limits, so `--max-cost` would bound only half of
        what a run spends.
        """
        async with limiters[model.id]:
            await budget.acquire()
            completion = None
            # Counted before the attempt, not after: a judge call that failed
            # still reached the provider, and this exists to answer "was
            # anything called", not "was anything billed".
            judge_calls[0] += 1
            try:
                request = CompletionRequest(model=model, messages=rendered)
                retry_kwargs = (
                    {} if model.max_retries is None else {"max_attempts": model.max_retries + 1}
                )
                provider = providers[model.id]
                completion = await call_with_retries(
                    lambda: provider.complete(request), **retry_kwargs
                )
                # Before the caller caches it, for the reason given in
                # _timed_call: the cache stores this object, so scrubbing only
                # what a record carries leaves the credential on disk in the
                # normal case. A judge quoting the output it graded is a
                # plausible way for one to arrive here.
                completion.text = redact(completion.text, secret_values)
                return completion
            finally:
                if completion is not None and completion.cost_usd:
                    judge_spend[0] += completion.cost_usd
                    # Persisted as it happens: finalize() never runs for a
                    # killed process, and a resume that cannot see this spends
                    # the ceiling again.
                    writer.record_spend(
                        judge_cost_usd=judge_spend[0],
                        unattributed_cost_usd=unattributed_spend[0],
                        judge_calls=judge_calls[0],
                    )
                await budget.release(
                    completion.cost_usd if completion else None, failed=completion is None
                )

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
            await budget.release(
                completion.cost_usd if completion else None, failed=completion is None
            )

    # Built after the limiters and budget, because a judge scorer calls a model
    # and has to go through both.
    scorecard = create_scorers(config, providers, call=_governed_call)
    # Only a user's own Python scorer decides what detail is safe to emit; every
    # other scorer explains itself by quoting the case content it looked at.
    keep_detail = frozenset(
        criterion.criterion for criterion, _ in scorecard if criterion.scorer.type == "python"
    )

    def recorded_id(case: Case) -> str:
        """The case id as it appears on disk for this run."""
        return _reported_case_id(case.id or "", privacy)

    # Set when the cost ceiling is hit, which ends the run without finishing
    # it — unlike --fail-fast, the remaining cells are still owed.
    budget_gone = [False]

    # A one-element list rather than a plain bool: `execute` and the cell
    # generator are separate closures and both need to see the same flag.
    stop_early = [False]

    async def execute(variant_name: str, model: ModelSpec, case: Case) -> "ResultRecord | None":
        record = ResultRecord(variant=variant_name, model=model.id, case_id=case.id or "")
        try:
            await _execute_cell(record, variant_name, model, case)
        except BudgetExhausted:
            # Never *finished*, so it leaves no record: writing one would count
            # it as a failure in the aggregates and, worse, mark it done for a
            # later resume — which is how the first version of this fix left
            # one cell permanently failed instead of all of them.
            #
            # But a cell refused at its judge has already made and paid for its
            # candidate call. Dropping the record must not drop the money with
            # it, or the ceiling stops holding across resumes.
            if record.cost_usd and not record.cached:
                # The in-run budget already counted this call when it
                # completed; what is missing is a durable record of it.
                unattributed_spend[0] += record.cost_usd
                writer.record_spend(
                    judge_cost_usd=judge_spend[0],
                    unattributed_cost_usd=unattributed_spend[0],
                    judge_calls=judge_calls[0],
                )
            budget_gone[0] = True
            stop_early[0] = True
            return None
        except Exception as exc:  # noqa: BLE001 - per-cell isolation: no cell may kill the run
            record.error = _describe_error(exc, safe=privacy.no_look)
        # The one place a record leaves this function. Both scrubs happen
        # here so that storage, callbacks, the progress display, reports, and
        # exports are structurally incapable of seeing what they should not —
        # rather than each having to remember.
        scrub_secrets(record, secret_values)
        if privacy.no_look:
            redact_record(record, keep_detail, hash_case_ids=not privacy.keep_case_ids)
        if budget_gone[0]:
            # Nothing more can be paid for, so stop handing out cells rather
            # than recording a wall of "skipped" failures for a matrix that
            # was never attempted.
            stop_early[0] = True
        if fail_fast and not cell_summary(record)[1]:
            # Set, not raised: cells already in flight finish and are recorded,
            # and the run finalizes normally. An exception here would discard
            # a partly-paid-for run to report the failure it was asked to find.
            stop_early[0] = True
        await _append(record)
        if on_result is not None:
            # A flaky progress callback must not abort an otherwise-healthy run.
            with contextlib.suppress(Exception):
                on_result(record)
        return record

    async def _execute_cell(
        record: ResultRecord, variant_name: str, model: ModelSpec, case: Case
    ) -> None:
        # Off-thread: rendering reads and hashes every media file the prompt
        # attaches, and those disk reads would stall every in-flight call.
        rendered = await asyncio.to_thread(
            render_messages, prompts[variant_name], case, config.base_dir
        )
        record.messages = serialize_messages(rendered)
        # Archiving inputs is bookkeeping: a full disk must not cost the cell.
        # Skipped in no-look mode, where the attachment is itself the data.
        if not privacy.no_look:
            with contextlib.suppress(OSError):
                for message in rendered:
                    for part in message.parts:
                        if isinstance(part, MediaRef):
                            await asyncio.to_thread(writer.store_artifact, part)

        if cache is not None:
            key = cache.key_for(model, rendered)
            async with single_flight(key):
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
            except BudgetExhausted:
                # Not a scoring failure: the ceiling was reached before this
                # judge could be called, so the cell is owed, not answered.
                # Scoring it 0 would write a record — counting the cell as a
                # quality failure in the aggregates and marking it done, so a
                # resume with a higher ceiling would skip it and the judge
                # would never run. Let it reach the cell handler, which drops
                # the record exactly as it does for a cell never started.
                raise
            except Exception as exc:  # noqa: BLE001 - a broken scorer fails the criterion, not the run
                entry.update(
                    score=0.0, passed=False, error=_describe_error(exc, safe=privacy.no_look)
                )
            record.scores[criterion.criterion] = entry

    async def _timed_call(record: ResultRecord, model: ModelSpec, rendered) -> Completion:
        # Per-model limits first: don't hold a cost-budget slot while queued
        # behind this model's own rate limit.
        async with limiters[model.id]:
            completion = await _budgeted_call(record, model, rendered)
        # Scrubbed here rather than on the record, because the response cache
        # stores this object first — and the cache is on by default, so
        # scrubbing only the record left the credential on disk in the normal
        # case while the tests, which disable the cache, saw nothing.
        completion.text = redact(completion.text, secret_values)
        return completion

    async def _append(record: ResultRecord) -> None:
        # Off-thread: appending is the one synchronous write on every cell's
        # path, and blocking the loop here throttles real concurrency.
        async with lock:
            await asyncio.to_thread(writer.append_result, record)

    # Generators, not lists: the matrix is never materialized, so a run over
    # hundreds of thousands of cells costs no more up front than a run over ten.
    # Cases are the outer loop for a source, because a source can only be
    # walked once; for a fixed list the original variant-major order is kept.
    if source_ref is not None:
        with source_errors(no_look=privacy.no_look):
            source = load_source(source_ref.source, config.base_dir, source_ref.params)

        async def factories():
            with source_errors(no_look=privacy.no_look):
                try:
                    async for case in iter_source_cases(
                        source, source_ref.page_size, source_ref.limit, config.base_dir
                    ):
                        for variant in variants_sel:
                            for model in models_sel:
                                if stop_early[0]:
                                    return
                                yield partial(execute, variant.name, model, case)
                finally:
                    await close_source(source)

        cell_stream = factories()
        expected_total = None
    else:

        def fixed_cells():
            for variant in variants_sel:
                for model in models_sel:
                    for case in cases:
                        # Checked as each cell is handed out rather than up
                        # front, which is what makes --fail-fast stop the run
                        # without cancelling work already paid for.
                        if stop_early[0]:
                            return
                        if (variant.name, model.id, recorded_id(case)) not in done:
                            yield partial(execute, variant.name, model, case)

        cell_stream = fixed_cells()
        expected_total = len(variants_sel) * len(models_sel) * len(cases)

    # Decide up front whether records will be handed back, so a large run never
    # accumulates a list it would only discard. A source has no known size, so
    # assume it is large rather than risk holding an unbounded run in memory.
    retain = expected_total is not None and expected_total <= MAX_RETAINED_RECORDS

    tally = _RunTally()
    for record in prior_records:
        tally.add(record)
    retained: list[ResultRecord] = list(prior_records) if retain else []

    def collect(record: ResultRecord | None) -> None:
        # None means the cell was never attempted (the cost ceiling); it is
        # not a result and must not reach the tally or the record list.
        if record is None:
            return
        tally.add(record)
        if retain:
            retained.append(record)

    await consume_bounded(cell_stream, settings.concurrency, collect)

    tally.judge_cost_usd = judge_spend[0]
    tally.judge_calls = judge_calls[0]
    tally.unattributed_cost_usd = unattributed_spend[0]
    counts, totals = tally.counts, tally.totals
    records = retained if retain else []

    warnings: list[str] = list(extra_warnings or [])
    if budget_gone[0]:
        warnings.append(
            "stopped at the cost ceiling with cells still to run — they were skipped, "
            "not failed, so the scores below cover only what was attempted. Resume with "
            "a higher --max-cost to finish it."
        )
    elif stop_early[0]:
        warnings.append(
            f"stopped early: --fail-fast, after {tally.total} of "
            f"{expected_total if expected_total is not None else 'an unknown number of'} cells"
        )
    if max_cost_usd is not None and budget.unknown_cost_seen:
        warnings.append(
            "--max-cost could not be enforced for every call: some models "
            "reported no cost (no built-in pricing and no params.pricing). "
            "Set params.pricing to track their spend."
        )

    aggregates = tally.aggregates
    gate = evaluate_gate(config.thresholds, aggregates["overall"], baseline_overall)
    writer.finalize(
        counts,
        totals,
        aggregates,
        asdict(gate) if gate else None,
        warnings,
        stopped_early=stop_early[0] and not budget_gone[0],
        # Not "complete": cells are still owed, and resume refuses a complete
        # run. A capped run used to wedge itself permanently.
        status="incomplete" if budget_gone[0] else "complete",
    )
    return RunResult(
        run_id=writer.run_id,
        path=writer.path,
        records=records,
        records_truncated=not retain,
        counts=counts,
        totals=totals,
        aggregates=aggregates,
        gate=gate,
        warnings=warnings,
        selection=selection,
        stopped_early=stop_early[0] and not budget_gone[0],
        incomplete=budget_gone[0],
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
    # An explicitly empty override — an unset variable in a CI script passing
    # `--baseline "$BASELINE"` — must fail rather than quietly disable the
    # regression gate, which is the thing the build is there to enforce.
    if override is not None and not str(override).strip():
        raise ConfigError("baseline was given an empty run reference")
    ref = override if override is not None else config.thresholds.baseline
    if not ref:
        return None
    if ref == "regression":
        ref = "baseline"
    return store.resolve_ref(ref)


def _ids_digest(ids: "Iterator[str] | list[str]") -> str:
    """A stable fingerprint of a selection of case ids."""
    digest = hashlib.sha256()
    for case_id in ids:
        digest.update(case_id.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _check_resumable_matrix(
    run_id: str, recorded: dict[str, Any] | None, current: dict[str, Any] | None
) -> None:
    """Refuse to finish a run with a different matrix than it started.

    The config fingerprint covers the config and every file it references, but
    not the flags: `--resume` alongside a different `--case`, `--model`, or
    `--variant` used to run whatever the new filters selected and then finalize
    the run as complete. With a sample it was worse — the draw is by position
    into the filtered list, so a resume over a different population produced a
    hybrid of two draws, a run whose cells came from two different case sets
    and whose numbers looked entirely ordinary.

    Identities, not counts. Comparing sizes alone closed only half of this:
    swapping two cases for two others, or one variant for another, leaves
    every count where it was.
    """
    if not recorded or not current or recorded == current:
        return
    changed = []
    for name in ("variants", "models"):
        was, now = set(recorded.get(name) or []), set(current[name])
        if was != now:
            changed.append(f"{name} {sorted(was)} → {sorted(now)}")
    if recorded.get("population") != current["population"]:
        # Only mention the sizes when they differ; "2 cells → 2" reads as a
        # contradiction of the sentence it is attached to.
        sizes = (
            f" ({recorded.get('count')} cells → {current['count']})"
            if recorded.get("count") != current["count"]
            else ""
        )
        changed.append(f"a different set of cases to draw from{sizes}")
    elif recorded.get("cases") != current["cases"]:
        changed.append(f"a different selection of cases ({current['count']} of the same set)")
    raise StorageError(
        f"run {run_id!r} covered a different matrix than this one does "
        f"({'; '.join(changed) or 'the selection changed'}), so resuming would finish it "
        "with cells from two different selections. Resume with the same filters the run "
        "started with, or start a fresh run."
    )


def _known_credentials(providers: dict[str, Any]) -> list[str]:
    """Every credential this run could accidentally persist.

    Values from a secrets file, plus the key each model resolved from the
    environment — the latter is not in the secrets list and is the usual case.
    """
    values: list[str] = []
    for provider in providers.values():
        values.extend(getattr(provider.env, "secret_values", ()) or ())
        values.append(provider.credential)
    # Deduplicated, order preserved, empties dropped.
    return list(dict.fromkeys(value for value in values if value))


def _prior_spend(store: RunStore, resume_run_id: str | None) -> dict[str, float]:
    """Spend the run being resumed already made and no record carries.

    Judge calls are not cells, and a cell dropped at the cost ceiling leaves
    no record on purpose, so neither appears in results.jsonl. Both are
    written to spend.json as they happen — a killed process never reaches
    finalize(), and reading only the finalized totals lost everything an
    interrupted run had spent.
    """
    if resume_run_id is None:
        return {"judge_cost_usd": 0.0, "unattributed_cost_usd": 0.0, "judge_calls": 0.0}
    spend = store.load_spend(resume_run_id)
    return {
        "judge_cost_usd": float(spend.get("judge_cost_usd") or 0.0),
        "unattributed_cost_usd": float(spend.get("unattributed_cost_usd") or 0.0),
        "judge_calls": float(spend.get("judge_calls") or 0.0),
    }


def _reported_case_id(case_id: str, privacy: "Privacy") -> str:
    """The id as it may be shown, which under no-look is the hashed one."""
    if privacy.no_look and not privacy.keep_case_ids:
        return hash_case_id(case_id)
    return case_id


def _resolve_sample(
    store: RunStore,
    resume_run_id: str | None,
    sample: int | None,
    sample_seed: int | None,
) -> tuple[int | None, int | None]:
    """Settle the draw before any case is chosen.

    A fresh sampled run gets a seed whether or not one was asked for, because
    a draw nobody can reproduce is not much of a draw — it goes into the run's
    metadata and comes back on the result.

    A resumed run reuses the draw recorded on the run it is resuming. Taking
    the caller's word instead would let the two halves of one run cover
    different cases, which produces no error and entirely plausible numbers.
    """
    if resume_run_id is None:
        if sample is not None and sample_seed is None:
            sample_seed = secrets.randbits(32)
        return sample, sample_seed

    recorded = store.load_meta(resume_run_id).get("selection") or {}
    prior_sample, prior_seed = recorded.get("sample"), recorded.get("seed")
    if sample is not None and sample != prior_sample:
        described = "did not sample" if prior_sample is None else f"sampled {prior_sample} cases"
        raise StorageError(
            f"run {resume_run_id!r} {described}, so it cannot be resumed with a sample "
            f"of {sample}. Resume takes the original run's draw; omit the sample to use it."
        )
    if sample_seed is not None and sample_seed != prior_seed:
        raise StorageError(
            f"run {resume_run_id!r} was drawn with seed {prior_seed}, not {sample_seed}. "
            "Resume takes the original run's draw; omit the seed to use it."
        )
    return prior_sample, prior_seed


def _load_baseline_overall(store: RunStore, run_id: str | None) -> dict[str, Any] | None:
    if run_id is None:
        return None
    meta = store.load_meta(run_id)
    aggregates = meta.get("aggregates")
    if not aggregates:
        raise StorageError(f"baseline run {run_id!r} has no aggregates (did it finish?)")
    return aggregates["overall"]


class _RunTally:
    """Counts, token/cost totals, and aggregates, accumulated one record at a time.

    Everything a finished run reports is a reduction over its records, so none
    of it requires holding the records themselves.
    """

    def __init__(self) -> None:
        self.total = self.succeeded = self.failed = self.cached = 0
        self.input_tokens = self.output_tokens = 0
        self.cost_usd = 0.0
        self.judge_cost_usd = 0.0
        #: Paid by cells whose record was dropped at the cost ceiling. Part of
        #: what the run cost, but attributable to no cell.
        self.unattributed_cost_usd = 0.0
        #: Judge calls this run actually made. Not derivable from cost — an
        #: unpriced judge model bills nothing and is still a call — and not
        #: from the cell counts, since a judge is not a cell. It exists so a
        #: caller can tell "nothing was called" from "the cells were cached".
        self.judge_calls = 0
        self._aggregator = Aggregator()

    def add(self, record: ResultRecord) -> None:
        self.total += 1
        self.failed += record.error is not None
        self.succeeded += record.error is None
        self.cached += bool(record.cached)
        self.input_tokens += record.input_tokens or 0
        self.output_tokens += record.output_tokens or 0
        # A cached cell made no call. Its record keeps the cost it *would*
        # have had, which is worth knowing, but the run's total is documented
        # as what the run actually cost — and a re-run served entirely from
        # cache reported the full price of the original.
        if not record.cached:
            self.cost_usd += record.cost_usd or 0.0
        self._aggregator.add(record)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "total": self.total,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "cached": self.cached,
        }

    @property
    def totals(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            # What the run actually cost: cells, judges, and cells that paid
            # for a call before the ceiling refused their judge. Per-cell costs
            # are cost_usd minus the other two, since neither is a cell.
            "cost_usd": round(self.cost_usd + self.judge_cost_usd + self.unattributed_cost_usd, 10),
            "judge_cost_usd": round(self.judge_cost_usd, 10),
            "unattributed_cost_usd": round(self.unattributed_cost_usd, 10),
            "judge_calls": self.judge_calls,
        }

    @property
    def aggregates(self) -> dict[str, Any]:
        return self._aggregator.result()


class BudgetExhausted(EvalingError):
    """The cost ceiling was reached, so this cell was never attempted.

    Distinct from a cell that failed: nothing was sent, nothing was spent, and
    the cell is still owed. Recording these as failures made a capped run read
    as a quality collapse — a pass rate computed over cells that never ran.
    """


class _CostBudget:
    """Admission control for --max-cost under concurrency.

    Naive check-then-call lets every in-flight coroutine pass the check before
    any cost lands, overshooting by concurrency × call cost. This bounds the
    overshoot: until the first call's cost is known, exactly one call may be
    in flight; afterwards a call is admitted only when spent + in-flight,
    projected at the costliest observed call, stays under the limit.
    """

    def __init__(self, limit: float | None, spent: float = 0.0):
        if limit is not None and (not math.isfinite(limit) or limit < 0):
            # `spent >= nan` is always False, so a NaN ceiling enforces
            # nothing while still counting as "a ceiling was given" — which is
            # what lets an unbounded source start. Checked here rather than at
            # each surface so the CLI and the MCP tool cannot drift.
            raise ConfigError(f"max cost must be a finite non-negative number, got {limit!r}")
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
                    raise BudgetExhausted(
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

    async def release(self, cost: float | None, *, failed: bool = False) -> None:
        """Return a slot. ``failed`` means the call raised rather than completing.

        A failed call tells us nothing about pricing, so it must not be
        mistaken for an unpriced one: doing so both warned that --max-cost
        "could not be enforced" on a fully-priced run that merely hit one
        error, and marked the budget knowable with no cost data, reopening the
        overshoot window the serial probe exists to close.
        """
        if self.limit is None:
            return
        async with self._cond:
            self.in_flight -= 1
            if not failed:
                # A completed call always makes the budget knowable, even when
                # its cost is unknown: an unpriced model contributes 0 to the
                # projection, so it must not throttle the run. (Requiring a
                # *priced* call here silently serialized every run against
                # local or unpriced models to concurrency 1.)
                self.any_landed = True
                if cost is None:
                    self._unknown_cost = True
            if cost is not None:
                self.spent += cost
                self.max_call_cost = max(self.max_call_cost, cost)
            self._cond.notify_all()


def _describe_error(exc: Exception, *, safe: bool = False) -> str:
    """Describe a failure. ``safe`` keeps only what cannot echo case content.

    Provider errors quote response bodies, and a rejected request often comes
    back with the offending input attached — so in no-look mode the message
    itself is a leak, and only the shape of the failure survives.
    """
    if safe:
        status = getattr(exc, "status_code", None)
        detail = f" (HTTP {status})" if status else ""
        return f"{type(exc).__name__}{detail} — detail withheld (no-look)"
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
    if isinstance(config.cases, CaseSourceRef):
        # The source's data is not a file and is not necessarily stable, so
        # only the code that produces it can be fingerprinted. Resume is
        # refused for source-backed runs precisely because this is not enough
        # to prove two runs saw the same cases.
        paths.add(str((config.base_dir / config.cases.source.rpartition(":")[0]).resolve()))
        return sorted(paths)
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


def select_variants_models(
    config: EvalConfig,
    *,
    models: list[str] | None = None,
    variants: list[str] | None = None,
) -> tuple[list[VariantSpec], list[ModelSpec]]:
    """Validate and apply the variant/model filters.

    Split out because a source-backed run needs exactly this and cannot have
    the case half, which requires knowing every case up front.
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

    # Matrix membership follows the declared role: a judge-only model is called
    # by its judge, never evaluated as a candidate.
    model_specs = [m for m in config.models if m.role in ("candidate", "both")]
    if models:
        known = {m.id for m in model_specs}
        unknown = sorted(set(models) - known)
        if unknown:
            judge_only = {m.id for m in config.models if m.role == "judge"} & set(unknown)
            hint = (
                f" ({', '.join(sorted(judge_only))} has role 'judge', so it is not evaluated)"
                if judge_only
                else ""
            )
            raise ConfigError(
                f"unknown model(s): {', '.join(unknown)}{hint} "
                f"(available: {', '.join(sorted(known))})"
            )
        wanted = set(models)
        model_specs = [m for m in model_specs if m.id in wanted]
    return variant_specs, model_specs


def estimate_run_cost(
    config: EvalConfig,
    variants: list[VariantSpec],
    models: list[ModelSpec],
    cases: list[Case],
    case_count: int | None = None,
) -> "CostEstimate | None":
    """What this matrix is likely to cost, before any of it runs.

    Renders one case per variant rather than all of them: the point is an
    order of magnitude to decide on, and rendering a 500,000-cell matrix to
    produce it would cost more than the decision is worth.

    ``case_count`` overrides the number of cases, for a source whose size is
    known from `limit` while ``cases`` holds only a sample.
    """
    if not cases:
        return None
    count = case_count if case_count is not None else len(cases)
    groups: list[tuple[str, dict[str, Any], int, int]] = []
    for variant in variants:
        try:
            rendered = render_messages(
                resolve_prompt(variant.prompt, config.base_dir), cases[0], config.base_dir
            )
        except Exception:  # noqa: BLE001 - an unrenderable prompt is reported elsewhere
            return None
        # The same approximation the mock provider uses; a real tokenizer per
        # provider would be more precise and no more useful at this altitude.
        input_tokens = sum(len(message.text) for message in rendered) // 4
        for model in models:
            groups.append((model.id, model.params, input_tokens, count))
    groups.extend(_judge_groups(config, len(variants) * len(models) * count))
    return estimate_run(groups)


def _judge_groups(config: EvalConfig, cells: int) -> list[tuple[str, dict[str, Any], int, int]]:
    """One group per llm-judge criterion: a judge is a billable call per cell.

    Leaving these out understated a judged run by roughly half, in the
    direction that matters — a scorecard with two judged criteria makes three
    calls per cell, not one.

    The judge's input is its rubric plus the output it is grading, which does
    not exist yet; the graded text is counted at the same assumed length the
    candidate's own output uses.
    """
    by_name = {model.id: model for model in config.models}
    groups: list[tuple[str, dict[str, Any], int, int]] = []
    for criterion in config.scorecard:
        if criterion.scorer.type != "llm-judge":
            continue
        judge = config.judges.get(criterion.scorer.params.get("judge", ""))
        model = by_name.get(judge.model) if judge else None
        if model is None:
            continue
        try:
            rubric = resolve_prompt(judge.rubric, config.base_dir)
        except EvalingError:
            continue
        # Rendered with an empty output: the fixed part of the rubric is what
        # we can count, and the graded text is added as an assumption.
        text = "".join(
            message.content
            if isinstance(message.content, str)
            else "".join(getattr(part, "text", "") for part in message.content)
            for message in rubric
        )
        graded_tokens = _assumed_output_tokens(config.models)
        groups.append((model.id, model.params, len(text) // 4 + graded_tokens, cells))
    return groups


def _assumed_output_tokens(models: list[ModelSpec]) -> int:
    """How long a candidate answer is assumed to be, for sizing a judge's input."""
    caps = [
        model.params["max_tokens"]
        for model in models
        if isinstance(model.params.get("max_tokens"), int) and model.params["max_tokens"] > 0
    ]
    return max(caps) if caps else ASSUMED_OUTPUT_TOKENS


def sample_cases(cases: list[Case], sample: int | None, seed: int | None) -> list[Case]:
    """A random subset of ``sample`` cases, in their original order.

    Order is preserved so that two runs of the same draw line up when read
    side by side; the randomness is in *which* cases, not where they land.
    Asking for more than exist takes them all rather than erroring — the point
    is "no more than this many", and a dataset that shrank shouldn't fail.
    """
    if sample is None:
        return cases
    if sample < 1:
        raise ConfigError(f"sample must be at least 1, got {sample}")
    if sample >= len(cases):
        return cases
    chosen = sorted(random.Random(seed).sample(range(len(cases)), sample))
    return [cases[index] for index in chosen]


def select_matrix(
    config: EvalConfig,
    *,
    models: list[str] | None = None,
    variants: list[str] | None = None,
    cases: list[str] | None = None,
    sample: int | None = None,
    sample_seed: int | None = None,
) -> tuple[list[VariantSpec], list[ModelSpec], list[Case]]:
    """Validate filters and return exactly what the matrix will execute.

    The single authority for matrix membership: the engine, dry runs, and the
    CLI's request count all use this, so they cannot disagree. Filtering
    models here does NOT remove judge providers — judges are not matrix
    members in the first place.
    """
    variant_specs, model_specs = select_variants_models(config, models=models, variants=variants)

    if isinstance(config.cases, CaseSourceRef):
        raise ConfigError(
            "this config's cases come from a source, which is fetched lazily and "
            "cannot be listed up front; use run_eval, or dry_run for a sample"
        )
    case_list = load_cases(config)
    if cases:
        # Under no-look the only ids a user has ever seen are the hashed ones,
        # so a raw id can never match and this listing always fired — printing
        # every raw id into a terminal or CI log, which is the disclosure the
        # mode exists to prevent. Accept either form, and echo back only the
        # form the user is entitled to see.
        shown = {_reported_case_id(case.id or "", config.privacy): case for case in case_list}
        known = {case.id for case in case_list}
        unknown = sorted(name for name in set(cases) if name not in known and name not in shown)
        if unknown:
            raise ConfigError(
                f"unknown case id(s): {', '.join(unknown)} (available: {', '.join(sorted(shown))})"
            )
        wanted = set(cases)
        case_list = [
            case
            for case in case_list
            if case.id in wanted or _reported_case_id(case.id or "", config.privacy) in wanted
        ]

    return variant_specs, model_specs, sample_cases(case_list, sample, sample_seed)


def _dry_run_source(
    config: EvalConfig,
    *,
    model_filter: list[str] | None,
    variant_filter: list[str] | None,
) -> "DryRunReport":
    variants_sel, models_sel = select_variants_models(
        config, models=model_filter, variants=variant_filter
    )
    prompts = {
        variant.name: resolve_prompt(variant.prompt, config.base_dir) for variant in variants_sel
    }
    _validate_media_support(variants_sel, models_sel, prompts)
    secret_env, _ = build_env(config.base_dir)
    providers = {
        model.id: create_provider(model, secret_env, config.base_dir) for model in config.models
    }
    create_scorers(config, providers)

    source_ref = config.cases
    with source_errors(no_look=config.privacy.no_look):
        source = load_source(source_ref.source, config.base_dir, source_ref.params)

    async def sample() -> tuple[list[Case], int | None]:
        try:
            total = await source_count(source)
            take = min(source_ref.page_size, source_ref.limit or source_ref.page_size)
            cases: list[Case] = []
            async for case in iter_source_cases(source, take, take, config.base_dir):
                cases.append(case)
            return cases, total
        finally:
            await close_source(source)

    with source_errors(no_look=config.privacy.no_look):
        cases, total = asyncio.run(sample())
    cells = []
    for variant in variants_sel:
        for model in models_sel:
            for case in cases:
                error = None
                try:
                    render_messages(prompts[variant.name], case, config.base_dir)
                except Exception as exc:  # noqa: BLE001 - reported per cell, like the engine
                    error = _describe_error(exc, safe=config.privacy.no_look)
                cells.append(
                    {
                        "variant": variant.name,
                        "model": model.id,
                        "case_id": _reported_case_id(case.id or "", config.privacy),
                        "error": error,
                    }
                )
    per_case = len(variants_sel) * len(models_sel)
    if total is not None:
        requests = per_case * (min(total, source_ref.limit) if source_ref.limit else total)
    elif source_ref.limit:
        requests = per_case * source_ref.limit
    else:
        requests = len(cells)  # only the sample is knowable
    return DryRunReport(
        requests=requests,
        cells=cells,
        sampled=total is None and not source_ref.limit,
        source_total=total,
    )


@dataclass
class DryRunReport:
    """What a run would do: the matrix, per-cell render outcomes, no model calls."""

    requests: int
    cells: list[dict[str, Any]]  # {variant, model, case_id, error: str|None}
    #: True when ``cells`` covers only a sample because the source's total is
    #: unknown — ``requests`` is then the sample size, not the run size.
    sampled: bool = False
    #: The source's own total, when it reports one.
    source_total: int | None = None

    @property
    def errors(self) -> list[dict[str, Any]]:
        return [cell for cell in self.cells if cell["error"] is not None]


def dry_run(
    config: EvalConfig,
    *,
    model_filter: list[str] | None = None,
    variant_filter: list[str] | None = None,
    case_filter: list[str] | None = None,
    sample: int | None = None,
    sample_seed: int | None = None,
) -> DryRunReport:
    """Validate everything a run needs — prompts, cases, scorers, rendering —
    without calling any model.

    A source-backed config is validated against a sample: the first page is
    fetched and rendered, which catches the errors that matter (a template
    referring to a variable the source doesn't provide, a missing attachment)
    without walking a production dataset to do it.
    """
    if isinstance(config.cases, CaseSourceRef):
        if sample is not None:
            raise ConfigError(
                "sampling cannot narrow a source-backed run: cases are fetched lazily, "
                "so there is no population to draw from. Use `limit` in the config to "
                "take fewer, or sample inside your source."
            )
        return _dry_run_source(config, model_filter=model_filter, variant_filter=variant_filter)
    variants_sel, models_sel, cases = select_matrix(
        config,
        models=model_filter,
        variants=variant_filter,
        cases=case_filter,
        sample=sample,
        sample_seed=sample_seed,
    )
    privacy = config.privacy
    prompts = {
        variant.name: resolve_prompt(variant.prompt, config.base_dir) for variant in variants_sel
    }
    _validate_media_support(variants_sel, models_sel, prompts)
    secret_env, _ = build_env(config.base_dir)
    providers = {
        model.id: create_provider(model, secret_env, config.base_dir) for model in config.models
    }
    create_scorers(config, providers)  # fail fast on bad scorer config

    cells = []
    for variant in variants_sel:
        for model in models_sel:
            for case in cases:
                error = None
                try:
                    render_messages(prompts[variant.name], case, config.base_dir)
                except Exception as exc:  # noqa: BLE001 - reported per cell, like the engine
                    # A render error quotes the template and the value it
                    # choked on, so under no-look it is case content.
                    error = _describe_error(exc, safe=privacy.no_look)
                cells.append(
                    {
                        "variant": variant.name,
                        "model": model.id,
                        # An id from a production system identifies a record as
                        # surely as the record does — the same reason a run
                        # hashes them.
                        "case_id": _reported_case_id(case.id or "", privacy),
                        "error": error,
                    }
                )
    return DryRunReport(requests=len(cells), cells=cells)
