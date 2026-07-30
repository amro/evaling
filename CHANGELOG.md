# Changelog

All notable changes to evaling are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **A relative `output_dir`/`cache_dir` now resolves against the eval config's
  directory**, not the working directory. Previously
  `evaling -c projects/foo/eval.yaml run` wrote to `./.evaling/runs`, so
  `cd projects/foo && evaling list` found nothing — the runs were somewhere
  else entirely, and nothing said so. Every other relative path in a config
  (prompts, datasets, attachments) already resolved against the config's
  directory; this was the exception. Defaults are included, so a config with
  no `settings:` block keeps its runs beside itself too.

  `--output-dir`, `--cache-dir`, and the `EVALING_*` variables are unchanged
  and still resolve against the working directory: you type those where you
  are standing. Absolute paths are never rewritten. **This moves where runs
  land for anyone who passes `-c` with a path outside their working
  directory**; pass `--output-dir` to keep the old location.

### Fixed

- **A malformed config produced a traceback instead of a message** in two
  cases, both found by the new fuzzing in `tests/test_config_fuzz.py`. A file
  that isn't valid UTF-8 — saved as UTF-16, or as latin-1 with an accent in it
  — died inside the codec, because `UnicodeDecodeError` is a `ValueError` and
  every read was guarded by `except OSError`. A file nested thousands of
  levels deep died inside PyYAML with `RecursionError`, which is not a
  `YAMLError`. Both now name the file and say what to do.

  Every user-authored file evaling reads — config, prompt, dataset, secrets,
  user config — now goes through one reader (`evaling.textfile`) rather than
  five hand-rolled ones that each caught a different subset. Malformed CSV
  (`csv.Error`: a NUL byte, an over-long field) is reported the same way
  instead of escaping.

### Added

- **Performance guards in CI** (`tests/test_performance.py`, marker `perf`,
  its own workflow job with coverage off). Memory flatness and throughput had
  been measured by hand once and then watched by nothing, so a 10x regression
  would have looked exactly like a passing suite. They assert shape rather
  than absolute speed — four times the cells must cost about four times the
  time, and nothing may be retained per cell — which holds on a shared runner
  where cells-per-second does not.

  Writing them showed the previous hand-written memory check was vacuous: it
  ran 4,000 cells against a retention cap of 10,000, so every record was held
  by design and the number it produced said nothing about the streaming path
  it was meant to protect. Retention is now measured by counting live
  `ResultRecord` objects, which is exact, and by comparing two run sizes,
  which cancels fixed overhead out.

- **`role` on a model: `candidate` (default), `judge`, or `both`.** A model a
  judge references must now declare its role, and the config is rejected with
  a message naming both ways out if it doesn't. Previously every model in
  `models:` was a system under test, so adding a judge silently doubled a
  run's cost and produced an aggregate row where the judge graded its own
  output. A run also now says which models are judging rather than being
  evaluated — the original defect was less the default than its invisibility.
  **Breaking:** existing configs with a judge need `role: judge` (or
  `role: both`) on the judge's model. A config where no model is a candidate,
  or where `role: both` is set on a model no judge uses, is also rejected —
  both would otherwise run or declare nothing.

- **Case sources** — cases can now be fetched from your own Python, a page at
  a time, instead of being listed in the config or a dataset file:
  `cases: {source: sources/prod.py:make_source, page_size: 200, limit: 5000}`.
  Implement `fetch(cursor, limit) -> CasePage` (sync or async); `count()` and
  `close()` are used when present. `CaseSource` is a Protocol, so no import or
  subclass is required. Cases stream, so run size is bounded by concurrency
  rather than by case count. See `docs/large-datasets.md`.
- **No-look mode** (`privacy: {no_look: true}`, or `evaling run --no-look`) —
  for evaluating production data nobody may read afterwards. Rendered prompts,
  model outputs, judge rationales, attachments, provider error bodies, and
  inline cases in the config snapshot are all dropped; scores, counts, and
  timings survive. Case ids are hashed by default, since an id from a
  production system identifies a record as surely as the record does
  (`keep_case_ids: true` to opt out). The response cache is disabled, because
  it stores prompts and completions verbatim. Redaction happens at one place —
  record construction — so every downstream surface is structurally incapable
  of leaking rather than individually careful.
- Two substantial examples: `examples/support-triage/` (a realistic
  classification eval with a weighted scorecard, a Python scorer, and a
  deterministic fake model behind the `command` provider) and
  `examples/no-look/` (a paging source over synthetic production data).
- Secrets file support: API keys may come from a gitignored
  `.evaling.secrets.yaml` beside the config, `~/.config/evaling/secrets.yaml`,
  or a path in `$EVALING_SECRETS` — the real environment always wins, so CI is
  unaffected. Keys are still never read from `eval.yaml`, are never written
  into `os.environ`, and are redacted from errors and stored artifacts. The
  file's permissions are checked (POSIX) and a warning is surfaced if it is
  readable by others. `evaling init` scaffolds an example and gitignores the
  real thing. See `docs/secrets.md`.
- Per-model limits: `max_concurrency` and `requests_per_minute`, composing
  with the global concurrency setting so a rate-limited hosted model no longer
  throttles the whole matrix.
- `evaling validate` — the same work as `run --dry-run`, under a name people
  look for.
- `evaling cache info` / `evaling cache clear [--older-than DAYS]` — inspect
  or prune the response cache.
- `evaling init --provider anthropic|openai|openai-compatible|mock` scaffolds
  a real model block for that vendor.
- `python -m evaling` as an alternative entry point.
- `GateResult` is exported from the top-level package; it is part of
  `run_eval`'s return type and could not previously be imported or annotated.
- Documentation: a full [tutorial](docs/tutorial.md) (install through CI
  gating), plus `docs/secrets.md`, `docs/troubleshooting.md`,
  `docs/python-api.md`, `docs/architecture.md`, a documentation index at
  `docs/README.md`, and `CONTRIBUTING.md`.
- The worked example evals moved from `tests/fixtures/e2e/` to
  [`examples/`](examples/) with a README. The test suite still runs all four
  end to end, so they cannot drift from the code.
- The documentation is now tested (`tests/test_docs.py`): YAML examples are
  validated against the real config schema, `docs/cli.md` is checked against
  actual `--help` output for undocumented commands and flags, and relative
  links must resolve.
- CI runs macOS and Windows in addition to Linux 3.10–3.13.

### Changed

- The engine no longer materializes the matrix. Cells stream through a fixed
  worker pool, so in-flight tasks are bounded by `concurrency` rather than by
  the number of cells, and aggregates are accumulated per record instead of
  computed from a retained list. Measured over a 30,000-cell run, live memory
  at the end is identical to before the run started.
- `RunResult.records` is empty for runs above `MAX_RETAINED_RECORDS` (10,000
  cells), with the new `records_truncated` flag set. Use the new
  `RunResult.iter_records()` to stream results from disk at any size. Empty
  rather than partial is deliberate: a partial list would silently produce
  wrong answers. **This is a behavior change for anyone reading `.records`
  from a very large run.**
- HTML reports degrade above 2,000 cells: aggregates and the gate stay
  complete, the per-case drill-down is limited to failing cases, and a notice
  says what was omitted and how to get it. A 50,000-cell report went from
  75 MB — large enough that a browser will not open it — to 5.5 KB.
- Prompt templates compile once per distinct source instead of once per cell.
  `Environment.from_string` recompiles on every call, so a 30,000-cell run was
  compiling the same template 30,000 times and keeping every result alive.
  Throughput improved ~21% (1,030 → 1,250 cells/sec against the mock provider).

### Added

- AAC audio (`.aac`). `.m4a` already covered AAC in an MP4 container; this adds
  raw ADTS streams.
- `evaling.concurrency.KeyedLocks`, a refcounted lock-per-key that drops locks
  when their last waiter leaves — the cache single-flight map previously kept
  one lock per distinct cell for the life of a run.
- `evaling.scoring.Aggregator` for incremental aggregation. `aggregate()` is
  now implemented on top of it, so there is one implementation of the
  arithmetic rather than two that can drift.

### Fixed

- Empty option values are no longer read as "use the default". `--baseline ""`
  quietly disabled the regression gate CI exists to enforce, `evaling run ""`
  and `-c ""` evaluated the default config, `--html ""` skipped the report, and
  `--out ""` fell back to stdout — all exiting 0. A script passing
  `--baseline "$BASELINE"` with the variable unset therefore got a green build
  that had checked nothing.
- `evaling list` could crash on a run whose stored timestamp contained markup:
  the shortened-timestamp path dropped the escaping the previous code had.

- `evaling run --resume ""` started a fresh run instead of failing. An unset
  shell variable in a CI script (`--resume "$RUN_ID"`) therefore bought a
  second full run rather than an error.
- `evaling list` elided run ids at 80 columns (`20260730T01…`), so the id every
  other command takes as an argument could not be copied from the listing that
  shows it. Run id and cost are now never truncated — a clipped `$0.00…` reads
  as roughly nothing when it may be `$0.0045` — and timestamps shorten instead.

- **Source-backed configs could not be run over MCP at all.** `run_eval` called
  `select_matrix` only to compute a progress total, which raises for a case
  source — so an agent got "cases come from a source ... use run_eval" while
  calling `run_eval`. Both headline features, large datasets and no-look, were
  unreachable from an agent. Progress is now indeterminate when the run size
  isn't knowable, and an unbounded source without `max_cost_usd` is refused
  rather than started.

- The MCP server reported the MCP SDK's version as its own, so an agent asking
  what it was connected to heard `1.28.1` rather than evaling's version.
- MCP tools reject an argument they don't declare, naming the ones they do
  take. Previously an unknown argument was accepted and silently dropped, so
  `run_eval` with a typo'd `config_path` ran the default config and reported
  success — the failure mode that looks like it worked, on the one tool that
  spends money. Only names are checked, so list and object arguments sent as
  JSON-encoded strings keep working.

- **No-look mode leaked case content on every failure path.** A failing
  `contains` criterion wrote the case's `expected` value into
  `scores[*].detail`, and a scorer that raised wrote the model output into
  `scores[*].error`. Redaction now whitelists — only a criterion scored by your
  own Python function keeps its `detail`, since every other scorer explains
  itself by quoting what it looked at — and criterion errors are replaced. The
  canary test missed this because it only ran cells that passed; it now
  exercises a failing scorer, a judge rationale, and a scorer that raises.
- **Duplicate criterion names are rejected.** They silently collapsed in a
  record's scores, and in no-look mode a name whitelisted for a Python scorer
  could carry another scorer's detail past redaction.
- **LLM-judge calls bypassed `--max-cost`** and the judge model's own
  concurrency and rate limits, because the scorer called its provider
  directly. A run with a $1.00 judge under a $0.05 ceiling completed for
  $20.20. Judge spend now counts against the budget, obeys the judge model's
  limits, and appears in `totals` as `judge_cost_usd` (with `cost_usd` now
  covering cells plus judges).
- **A fatal error left workers running.** `asyncio.gather` propagates the first
  exception without cancelling its siblings, so the pool kept pulling new cells
  and issuing paid calls after a run had already failed — 197 of 200 cells in a
  loop that outlived the failure. Workers are now cancelled and awaited first.
- **A failed call was treated as an unpriced one**, so a single transient error
  on a fully-priced run warned that `--max-cost` "could not be enforced", and
  marked the budget knowable with no cost data.
- A bad scorecard no longer leaves an unfinalized run directory behind: the
  scorecard is validated before anything is written.

- Usage numbers in a completion (`input_tokens`, `output_tokens`, `cost_usd`)
  are now validated where the completion is built, so a `command` script
  emitting `"input_tokens": "12"` is coerced and junk like `"twelve"` fails
  that one cell with a clean provider error. It previously crashed the whole
  run with a raw `TypeError` when the totals were summed.
- Cancelling a `command` provider call mid-run (Ctrl-C, a failing sibling)
  now kills and reaps the child process instead of leaving it running.
- Sync user code — a source's `fetch`/`count`/`close` and `python` scorer
  functions — now runs off-thread, so a scorer or source doing real I/O no
  longer stalls every in-flight model call. Media attachments are read and
  base64-encoded off-thread too, both when rendering and inside the
  `anthropic`/`openai` providers, and the OpenAI audio path no longer reads
  each file twice.
- `iter_results` now actually streams: it read the entire results file into
  memory first, which defeated its purpose on the large runs it exists for.
- `store_artifact` copies via temp + rename like every other storage write.
  A crash mid-copy used to leave a partial artifact that the idempotency
  check then trusted forever.
- A dataset row whose reserved field starts with `file://` (say,
  `expected: "file:///etc/hosts"`) keeps it as a literal value. It used to
  become an attachment named `expected`, with `expected` silently unset.

- The totals line in HTML reports (`72/72 succeeded · …`) overlapped the
  bottom row of the summary table. It carried a negative top margin, so the
  text and the table's last border rendered on top of each other.

- The `run` progress bar crashed with an `UnboundLocalError` for source-backed
  configs, which have no up-front cell count.
- The `command` provider now runs its script in the config's directory rather
  than inheriting the caller's working directory. Every other path in a config
  resolves against the config file, so `command: python3 model.py` used to be
  the one setting that silently depended on where you were standing.

- **All file I/O now names UTF-8 explicitly.** Python previously fell back to
  the platform default encoding, which is cp1252 on Windows — so writing an
  HTML report crashed, and any run whose output contained an emoji, a curly
  quote, or CJK text would have failed while recording results. Files evaling
  writes for itself also pin `newline="\n"`, so JSONL and JSON are
  byte-identical across platforms. A lint rule (PLW1514) now makes unencoded
  text I/O a build error.
- The secrets-file permission check is POSIX-only. Windows synthesizes mode
  bits that always look world-readable, which produced a spurious warning on
  every run there.

- MCP server (`evaling mcp`, optional extra `evaling[mcp]`): `run_eval`,
  `get_run`, `get_case_result`, `compare_runs`, `list_runs`, `set_baseline`,
  and `render_prompt` over stdio, for agent-driven prompt iteration. Responses
  are token-frugal — summaries by default, pagination, snipped outputs — and
  every tool is a thin call into the same core the CLI uses.
- HTML reports: `--html PATH` on `run` and `compare`, and `--format html` on
  `export`. A single self-contained file — inline styles, no JavaScript, no
  network — with the summary matrix, gate verdict, and per-case drill-down
  (outputs, criterion breakdown with judge rationales, and the exact prompt
  sent). Failing cases first, CSS-only failures-only toggle, model output
  escaped, media referenced by hash rather than inlined.
- Real providers: `anthropic` (Messages API), `openai` (chat completions),
  `openai-compatible` (any OpenAI-format endpoint — Ollama, vLLM, LM Studio,
  OpenRouter, Gemini's compatibility endpoint), and `command` (any CLI or
  script, request on stdin / response on stdout). All over httpx, tested
  against a faked transport — the suite still makes no network calls.
- Cost tracking: built-in per-model pricing for Anthropic models, plus
  `params.pricing` to supply or override rates for any model.
- `docs/providers.md` — every provider, per-model options, pricing, errors and
  retries, and how to add a provider.
- The CLI: `run` (progress, matrix filters, `--dry-run`, `--max-cost`,
  `--resume`, baseline gating, large-matrix confirmation), `show`
  (summary/failures/case drill-down), `list`, `compare` (per-cell deltas),
  `export` (json/csv/md), `baseline set/show`, and `init` (offline runnable
  scaffold). Global `-c/-o/--cache-dir/--no-color/-q/-v/--json` flags; exit
  code 1 on gate failure, 2 on config errors. Run references: id, label,
  `latest`, `baseline`.
- `EVALING_USER_CONFIG` env var to relocate the user config file.
- `docs/getting-started.md`, `docs/cli.md`, and `docs/ci.md`.
- Scoring: built-in scorers (`exact`, `contains`, `not-contains`, `regex`,
  `json-valid`, `json-schema`, `python`, `agreement`) plus `llm-judge`
  autoraters — text rubrics receiving `output`/`expected`/`vars`, JSON verdicts
  with scale normalization. Scorecard aggregation (weighted per-cell scores,
  overall and per variant×model stats in `run.json`) and threshold gating
  (`min_pass_rate`, `min_score`, baseline regression).
- `docs/scoring.md` and `docs/evaluating-judges.md` (the meta-eval recipe).
- `video` content part type (`.mp4`, `.mov`, `.webm`) — typed and stored like
  other media; currently exercised by the mock provider only.
- End-to-end fixture evals (`tests/fixtures/e2e/`): four complete sample
  projects — text single-turn, text multi-turn, media single-turn (image, PDF,
  audio, video via CSV `file://`), media multi-turn — run through the real
  engine in tests, including full-cache re-runs and artifact dedup checks.
- Run engine: executes the variants × models × cases matrix with bounded
  concurrency, retries, per-cell failure isolation, and resume for interrupted
  runs. Exported as the `evaling.run_eval` programmatic API.
- Run storage: plain-file run directories (`run.json`, config snapshot,
  append-as-completed `results.jsonl`, content-addressed `artifacts/`).
- Response cache (on by default): requests keyed by model spec and
  content-addressed messages, so identical requests — including
  moved-but-identical media files — are served from disk.
- `docs/storage.md` — run format, resuming, caching, programmatic access.
- Provider interface: async, pluggable `Provider` abstraction with a registry,
  typed completions (text, token usage, cost), and retryable-vs-fatal errors.
- Deterministic mock provider for tests and dry runs: echoes the last user
  message (with media hash markers), fixed responses, and simulated
  transient/fatal failures.
- Retry with exponential backoff for transient provider failures, and
  bounded-concurrency execution for parallel model calls.
- CI status badge in the README.
- Prompt rendering: Jinja2 templating with strict undefined (typos fail
  loudly), case vars as top-level names, attachments as `files.<name>`.
- Multimodal content resolution: images, PDFs, and audio referenced from
  prompts or cases, typed by extension, validated against the part type, and
  sha256-hashed by content.
- Case datasets: CSV/JSONL loading with reserved fields (`id`, `expected`,
  `human_label`, `files`), the `file://` attachment convention, and generated
  unique case ids. External prompt files as YAML message lists.
- `docs/prompts.md` — prompts, templating, multimodal inputs, and datasets.
- Config schema and loader for `eval.yaml`: strict validation (unknown keys
  rejected), multi-turn messages with typed content parts (text/image/file/audio),
  scorecard with weighted criteria, judge definitions, thresholds, and readable
  error messages naming the file and offending fields.
- Layered workspace settings: CLI flags > `EVALING_*` environment variables >
  eval config `settings:` > `~/.config/evaling/config.yaml` > defaults.
- `docs/configuration.md` — full configuration reference.
- Project scaffold: uv-managed Python package, `evaling` CLI entry point with
  `--version`, pytest test suite, ruff lint/format, GitHub Actions CI.
- MIT license.
- Requirements document (`REQUIREMENTS.md`) covering the settled v1 design.
