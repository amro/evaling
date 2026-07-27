# Runs, storage, and caching

Every run is persisted locally as plain files — inspectable, versionable, and
the source of truth that all exports and reports are rendered from.

## Run directory layout

Runs live under the output directory (default `.evaling/runs/`, see
[configuration](configuration.md#settings-resolution)):

```
.evaling/runs/<run-id>/
  run.json               # metadata and aggregates
  config.snapshot.yaml   # the exact config used, canonically serialized
  results.jsonl          # one record per variant×model×case
  artifacts/             # binary inputs, content-addressed
```

Run ids are timestamp-prefixed with millisecond precision
(`20260724T141530042-9f2a`). Ordering (for `list` and the `latest` reference)
comes from the nanosecond creation time recorded in `run.json`, so two runs
created in the same millisecond still order correctly.

## `run.json`

```json
{
  "id": "20260724T141530-9f2a",
  "label": "tightened-rubric",
  "status": "complete",
  "started_at": "2026-07-24T14:15:30Z",
  "finished_at": "2026-07-24T14:15:41Z",
  "config_sha256": "…",
  "counts": {"total": 8, "succeeded": 7, "failed": 1, "cached": 4},
  "totals": {"input_tokens": 5120, "output_tokens": 890, "cost_usd": 0.023}
}
```

`status` is `running` until the run finalizes — a crashed or interrupted run
keeps that status, which is how unfinished runs are recognized.

## `results.jsonl`

One JSON object per line, one line per matrix cell, **appended as each cell
completes**. Fields: `variant`, `model`, `case_id`, `messages` (the rendered
conversation actually sent, media parts content-addressed), `output`,
`input_tokens`, `output_tokens`, `cost_usd`, `latency_ms`, `cached`, `error`,
and `scores` (per-criterion results).

Failure isolation: a cell that fails to render or whose model call fails (after
retries) records its `error` and the run continues. Aggregates count it as
failed; the other cells are unaffected.

## Resuming

Because results append as they complete, an interrupted run keeps everything
finished so far. Resuming (`--resume <run-id>`) re-executes only the cells with
no recorded result. Cells that completed *with an error* are not re-executed —
resume means "finish what was interrupted", not "retry failures".

Guarantees and limits:

- A truncated final `results.jsonl` line (the artifact of a process killed
  mid-write) is detected and dropped on open; corruption anywhere else in the
  file is a hard error.
- Resume requires the **exact config the run started with** (verified against
  the run's recorded config hash) and refuses runs already marked complete.
- One process per run directory: resuming the same run from two processes at
  once is unsupported and can record duplicate cells.

## Artifacts

Binary inputs (images, PDFs, audio) are copied into `artifacts/` named by their
content hash (`<sha256>.<ext>`). Ten cases referencing the same image store one
artifact; results reference content hashes, so a stored run is self-contained
and reproducible even if the original files move.

## Response caching

With caching on (the default), each request's key is a hash of the model spec
(provider, id, params, endpoint fields) plus the rendered messages, with media
addressed by content hash. Identical requests are served from the cache
directory (default `.evaling/cache/`) — so re-running to iterate on scorers or
after an interruption costs nothing for already-answered cells.

- A moved-but-identical media file is still a cache hit; changed bytes miss.
- Cached results have `"cached": true` and no latency.
- Bypass with `--no-cache` or `EVALING_CACHE=false`; unreadable cache entries
  degrade to misses, never errors.
- Identical requests in the same run are single-flighted: duplicates wait for
  the first call and reuse its cached response instead of paying again.

## Programmatic access

The engine is a public API — everything the CLI does is available in Python:

```python
from evaling import run_eval
from evaling.config import load_config

result = run_eval(load_config("eval.yaml"))
print(result.run_id, result.counts, result.totals)
for record in result.records:
    print(record.variant, record.model, record.case_id, record.output)
```

## Large runs

Above 10,000 cells (`evaling.engine.MAX_RETAINED_RECORDS`) a run stops handing
every record back in memory: `RunResult.records` is empty and
`records_truncated` is set. Counts, totals, and aggregates are unaffected —
they're accumulated as the run proceeds rather than computed from a retained
list. Everything is still on disk:

```python
for record in result.iter_records():  # streams, any run size
    ...
```

Empty rather than partial is deliberate. A partial list looks usable and would
silently give you wrong numbers.

## Source-backed runs cannot be resumed

When cases come from a [case source](large-datasets.md), `--resume` is refused.
Resume relies on the config fingerprint proving both runs saw the same cases,
and a live source can return different rows on the second call — inserted,
mutated, aged out, or simply a moved time window. The resulting run would
describe two different populations while looking entirely normal.

File- and inline-backed runs are unaffected: their data is fingerprinted, so
resume stays supported there.

## What no-look mode stores

With `privacy.no_look`, `results.jsonl` holds scores, counts, tokens, cost,
latency, and hashed case ids — no prompts, outputs, judge rationales, or
attachments. `artifacts/` stays empty, `config.snapshot.yaml` has inline cases
stripped, and the response cache is disabled for the run. See
[no-look.md](no-look.md).
