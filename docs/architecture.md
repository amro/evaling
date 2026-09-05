# Architecture

How evaling is put together, and why. Aimed at contributors and at anyone
extending it.

## The shape of a run

```
eval.yaml ──▶ config ──▶ matrix ──▶ engine ──▶ scorers ──▶ aggregates ──▶ gate
                 │                     │                        │
              cases              providers ◀── cache        storage ──▶ report
```

1. **Load and validate** the config and prompt files; cases come from the
   config, a dataset file, or a source that is paged lazily.
2. **Expand the matrix**: every variant × model × case is one *cell*. The
   matrix is a generator, never a list.
3. **Render** each cell's prompt through Jinja2 with the case's variables.
4. **Call** the model, subject to concurrency, rate, and cost limits.
5. **Score** the output against the scorecard.
6. **Persist** each result the moment it lands.
7. **Aggregate**, evaluate thresholds, and render.

Steps 3–6 run concurrently per cell; everything else is sequential.

## Modules

| Module | Responsibility |
| --- | --- |
| `config/` | Schema, loading, settings layering, case datasets |
| `sources.py` | Case sources: the `CaseSource` protocol and page iteration |
| `privacy.py` | No-look redaction, at one boundary |
| `render.py`, `templating.py` | Jinja2 rendering with `StrictUndefined` |
| `content.py` | Attachments: media typing, encoding, capability checks |
| `engine.py` | The matrix run: scheduling, limits, resume, budget |
| `providers/` | One class per backend, over `httpx` |
| `scorers/` | One class per scorer type |
| `scoring.py` | Weighted scorecards, aggregates, run comparison |
| `storage.py` | Run directories, `results.jsonl`, atomic writes |
| `cache.py` | Content-addressed response cache |
| `secrets.py` | Key resolution and redaction |
| `limits.py` | Per-model concurrency and rate limiting |
| `report.py`, `export.py` | HTML, JSON, CSV, Markdown output |
| `cli/` | Click commands and terminal rendering |
| `mcp_server.py` | The same operations over MCP |

The dependency direction is one-way: `cli` and `mcp_server` sit on top of
`engine`, which sits on `providers`, `scorers`, and `storage`. Nothing lower
imports anything higher, so the CLI is a client of the public API rather than
the place logic lives.

## Design decisions

### The engine is async; everything a user writes can be sync

An eval is almost entirely waiting on network calls, so the engine is
`asyncio` throughout with a bounded semaphore over cells. Scorers and the
`command` provider may be written sync — they're run in a thread when they
are. Users writing a Python scorer shouldn't have to learn async to grade a
string.

### httpx, not vendor SDKs

Providers are thin classes over `httpx` rather than each vendor's SDK. Vendor
SDKs disagree about retries, timeouts, and error types, and pull in
dependencies that need auditing. One HTTP client means one retry policy, one
timeout story, one place where secrets are redacted, and a small dependency
tree.

The cost is that new vendor features need explicit support. That trade favors
a tool whose job is *comparing* providers uniformly.

### Nothing is materialized

The matrix is a generator, not a list, and cells stream through a fixed worker
pool — so in-flight work is bounded by `concurrency` rather than by cell
count. Aggregates accumulate per record (`scoring.Aggregator`) instead of
being computed from a retained list, and records above a cap aren't kept at
all. A case source extends the same idea to the cases themselves, retaining
one page plus in-flight cases. Its cursor-cycle detector still keeps one
cursor per fetched page. MCP result inspection streams from disk and retains
only the requested rows, even when every result failed.

`aggregate()` is implemented on top of `Aggregator`, so there is one
implementation of the arithmetic rather than two that can drift apart.

### Redaction happens before public surfaces

No-look mode strips case content the moment a cell finishes scoring, before
the record reaches storage, callbacks, display, reports, or MCP. Everything
downstream is then structurally unable to leak it. The alternative — asking
each subsystem to remember — works until the seventh one forgets. Source
errors bypass cell records, so the engine also withholds their diagnostics
and exception chains under no-look, including during dry-runs.

Scorers deliberately sit *before* that boundary: a scorer sees the real output
and emits a verdict plus whatever detail its author deems safe, which puts the
judgment where the domain knowledge is. Judge rationales are the exception and
are dropped, since a rationale quotes what it graded.

### Results are written as they complete

`results.jsonl` is appended per cell, not written at the end. A run killed
partway keeps everything it finished, which is what makes `--resume` possible
and what makes a long expensive run safe to interrupt.

Everything else — `run.json`, artifacts — is written temp-then-rename, so a
reader never sees a partial file. Reading a run never repairs or mutates it;
recovery happens only on the write path, where a torn final line from a hard
kill is detected and discarded.

### Cache keys cover only what changes the answer

The key hashes provider, model, base URL, the `command` string, request
parameters, and the rendered messages. It deliberately excludes run labels, output directories, and
concurrency: those change what you call a run, not what the model returns.
Cached responses you paid for survive edits that couldn't have changed them.

Per-key locks mean N cells needing the same uncached response make one call,
not N.

### Cost control is admission control

`--max-cost` is checked before issuing a call, not after totaling one. A
post-hoc check tells you what you already spent. The budget also tracks calls
whose cost is unknown (a provider that returned no usage), so an unpriced
model can't quietly slip past the ceiling.

### Limits compose

A call must satisfy the global concurrency semaphore, its model's concurrency
cap, and its model's rate limit. Per-model limits are acquired *outside* the
cost budget, so a cell queued behind a rate limit isn't holding a budget slot
it can't use.

### Reports have no JavaScript

The HTML report is one self-contained file: inline CSS, no scripts, no
external requests. The failures-only filter is a CSS checkbox and sibling
selector. It survives email clients, CI artifact viewers, and strict CSPs, and
there's no XSS surface — all untrusted text is escaped on the way in.

### Untrusted text is escaped everywhere it's displayed

Model output, config-supplied names, and provider error messages are attacker-
influenced in the sense that matters: they contain whatever they contain. They
are escaped for rich markup in the terminal and for HTML in reports. A model
that emits `[/bold]` used to crash the CLI; now it prints.

## Adding things

**A provider**: subclass `HttpProvider` (or `Provider` for something that
isn't HTTP), declare `SUPPORTED_MEDIA` and `DEFAULT_API_KEY_ENV`, implement
`complete()`, and register it. See [providers.md](providers.md).

**A scorer**: subclass `Scorer`, implement `score()`, return a `ScoreResult`,
and register the type. Bounds are validated at construction, so a scorer
cannot emit a NaN or an out-of-range score into an aggregate.

**A CLI command**: add it under `cli/`, calling the public API in
`evaling/__init__.py` — if the operation isn't available there, add it there
first so the MCP server and Python users get it too.

## Testing

The suite is offline and hermetic: no network, no API keys, no wall-clock
sleeps. Provider tests use `httpx.MockTransport` with realistic payloads;
limiter tests inject a virtual clock; concurrency tests measure real peak
in-flight calls rather than asserting on implementation details.

The examples in [`examples/`](../examples/) are run end to end on every
commit, so they can't drift from the code.

See [CONTRIBUTING.md](../CONTRIBUTING.md) to run any of it.
