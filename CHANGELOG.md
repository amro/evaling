# Changelog

All notable changes to evaling are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Copy-pasteable MCP client configuration** per client (Claude Code, Claude
  Desktop, Cursor, generic) in `docs/mcp.md`, including the case that actually
  bites: a desktop app launches the server from wherever the app is, not from
  your project, so `evaling mcp` with no arguments looks for `eval.yaml` in the
  wrong place. Use `cwd` or an absolute `-c`.

- Documented that `evaling --json list` and the MCP `list_runs` tool return
  different shapes — a bare array keyed `id` versus
  `{"runs", "total", "baseline"}` keyed `run_id` — so a script written against
  one does not read the other. A test pins both so the documentation cannot
  quietly become wrong.

- **`examples/rag-pipeline/`** — a worked `command`-provider example where the
  thing under test is a system rather than a prompt: retrieve from a corpus,
  then answer. The only existing `command` example was a keyword classifier,
  which demonstrated the wire protocol but not the point.

  It discriminates on two axes — the prompt, which changes pipeline behaviour
  rather than wording, and the retrieval configuration, which is a matrix
  dimension. Offline and deterministic; swap one function for a real model
  call. See its README for what each axis demonstrates.

- **`evaling calibrate --from-run RUN --labels FILE`** — scaffolds an eval
  that measures how well a judge agrees with you, from a run you already made
  plus your ratings of its outputs. `docs/evaluating-judges.md` described the
  config shape; this writes it. It generates only — no model is called and
  nothing is spent — producing your rated answers as cases, two deliberately
  different rubric phrasings as variants, and the `agreement` scorer grading
  each verdict against your rating. Cases with no rating are left out and
  counted out loud; ratings matching no case in the run are an error rather
  than an empty file.

- **`--log-requests PATH`** — a JSONL trace of every provider call: the
  request body sent, the response received, the status, and the elapsed time.
  For the `command` provider it records exit code, stdout, and stderr, which
  is otherwise invisible. The alternative was adding print statements to
  evaling and running it from a checkout.

  Headers are never written — the API key travels in one, so the way to
  guarantee the file cannot contain it is to have no code path that writes
  them — and every credential evaling knows about, from a secrets file or
  resolved by a model from the environment, is scrubbed from the bodies too,
  for a gateway that reflects credentials into an error. Refused under no-look,
  where a verbatim record of prompts and completions is the exact artifact
  that mode exists to prevent.

- **`evaling doctor`** — version, Python, platform, the config it found, every
  resolved setting *with the layer that supplied it*, which secrets file is in
  play and which variables it defines, whether each model's API-key variable
  resolves, and cache/run-store sizes. The bug-report recipe was three
  commands whose combined output still omitted the things that actually go
  wrong.

  It touches no network and works when nothing else does: a missing config, a
  config that will not parse, or a broken secrets file are all reported rather
  than raised. `--check-providers` opts into one minimal call per model to
  check credentials, which is the only part that spends anything. Exits 1 on
  findings, so it works as a setup check in a script. Secrets are described,
  never printed — the file's path and the *names* of the variables it defines,
  so its output is safe to paste into an issue.

- **The large-matrix confirmation now applies over MCP.** `run` asks before
  100+ model calls, but only at a terminal — so CI skipped it deliberately
  (a prompt would hang the build; `--max-cost` is the guard there) and MCP
  skipped it entirely, leaving the surface with the least human supervision as
  the one with no ceiling. `run_eval` now refuses a matrix that size unless
  `max_cost_usd` or `confirm_large: true` is passed, or `sample` brings the
  cell count under the threshold. The error names all three.

- **`--fail-fast`** on `run` (and `fail_fast` over MCP): stop at the first
  failing cell instead of paying for the rest of the matrix. The stop is
  graceful — cells already in flight finish and are recorded, and the run
  finalizes, so the partial run is readable with `evaling show`. It exits `1`
  whether or not thresholds are configured, since a build that stopped early
  but exited `0` would read as a pass. `stopped_early` appears in the run
  metadata, the `--json` output, and the MCP summary.

- **`--sample N`** on `run` and `validate` (and `sample` over MCP): evaluate a
  random N of the selected cases. The fast loop while a prompt is still
  moving, instead of hand-listing `--case` ids.

  Every sampled run reports and stores the seed that produced its draw, so a
  draw can be repeated exactly — `--sample 20 --sample-seed 2894127714`. That
  matters for comparison: two sampled runs with different seeds differ partly
  by prompt and partly by which cases they happened to draw. `--resume`
  continues the original draw rather than making a new one, and refuses a
  conflicting `--sample`/`--sample-seed` rather than quietly producing a run
  whose halves cover different cases. Not available for source-backed runs,
  which have no population to draw from — use `limit`.

  `evaling compare` and the MCP `compare_runs` tool now warn when the two runs
  did not cover the same cases — a sampled run against a full one, two
  different draws, or two runs narrowed with different `--case` filters. The
  warning reaches the CLI, `--json`, the MCP response, and the HTML report,
  which is the copy people share. A comparison attributes every delta to
  whatever you changed, and that reading is wrong when the case sets differ —
  in a way that leaves the numbers looking entirely plausible.

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
  [`examples/`](examples/) with a README. The test suite still runs them all
  end to end, so they cannot drift from the code.
- The documentation is now tested (`tests/test_docs.py`): YAML examples are
  validated against the real config schema, `docs/cli.md` is checked against
  actual `--help` output for undocumented commands and flags, and relative
  links must resolve.
- CI runs macOS and Windows in addition to Linux 3.10–3.13.

- AAC audio (`.aac`). `.m4a` already covered AAC in an MP4 container; this adds
  raw ADTS streams.
- `evaling.concurrency.KeyedLocks`, a refcounted lock-per-key that drops locks
  when their last waiter leaves — the cache single-flight map previously kept
  one lock per distinct cell for the life of a run.
- `evaling.scoring.Aggregator` for incremental aggregation. `aggregate()` is
  now implemented on top of it, so there is one implementation of the
  arithmetic rather than two that can drift.

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

- **Two harnesses that cover a class instead of an instance**, after five
  rounds of finding the same shapes one at a time.

  `tests/test_surface_parity.py` drives one scenario table through both the
  CLI and the MCP server and asserts they agree about what they refuse. The
  surfaces had diverged five separate times — the large-matrix confirmation,
  `sample_seed` without `sample`, source-backed refusals, `--yes` against an
  unbounded source, `list --limit` clamping — each found by a person, after
  shipping. Deliberate differences are listed with their reasons, so "missing"
  reads differently from "decided".

  `tests/test_hostile_content.py` builds one run whose every text field is
  hostile — unbalanced rich markup, a spreadsheet formula, a script tag, a
  markdown table break, a credential, an RTL override — and drives every
  reading command and every MCP tool over it, asserting nothing crashes,
  nothing is interpreted, and no secret survives. Adding a command means
  adding one line.

### Fixed

- **`render_prompt`'s config argument is now `config_path`**, like every other
  MCP tool; it had carried the wrapper's own variable name, so an agent
  passing the argument that works everywhere else was told it was unknown.
  `list --limit 0` and negative limits now clamp on the CLI as they already
  did over MCP, and `confirm_large` acknowledges an unbounded source the way
  `--yes` does on the CLI.

- **A credential returned *by* a model was stored verbatim** — including in
  the response cache, which is on by default. `results.jsonl`,
  exports, and reports are shared and attached to issues, and a gateway
  echoing a header or a `command` wrapper printing its environment puts a key
  in the model's output — not just in an error, which was the only path
  covered. Every credential a run knows about is now scrubbed from stored
  output, errors, and scorer details. The scrub happens as the completion
  returns rather than on the record, because the cache stores the completion
  first — scrubbing the record alone left the key in `.evaling/cache/` on the
  default path while the tests, which disable caching, saw a clean run.

- **A pinned baseline pointing at an unfinished run failed the run after full
  spend**, contradicting the "must fail before any model call" intent — the
  baseline's aggregates were only read once every cell had executed, and the
  run was then left wedged in status `running`.

- **`evaling show` and `validate` crashed on markup in run metadata or model
  output.** A run labelled `[/bold]x`, or a model that emitted one, made the
  commands whose whole job is reading a run back die with a `MarkupError`
  traceback. Labels, model output, and dry-run cell names are now escaped.

- **The large-matrix confirmation skipped source-backed runs.** It lived only
  in the inline-cases branch, so `limit: 150` started unprompted at a terminal
  while the MCP server refused the identical run.

- **CSV export was formula-injectable through case id, variant, and model.**
  Only the model's output and error were escaped, but a case id comes from the
  dataset, which is as external as anything the model says.

- `parse_json_lenient` was quadratic on adversarial model output — 16 KB of
  `[` took five seconds while holding a concurrency slot — because every
  bracket restarted a full parse. Now bounded to the first 64 candidates.

- **Resuming a no-look run re-ran and re-billed every finished cell.** Records
  are stored with hashed case ids, but the resume skip-set was built from raw
  ids, so nothing ever matched: a two-cell run resumed produced four records,
  doubled counts, and doubled spend.

- **Hitting `--max-cost` wedged the run.** Cells the ceiling skipped were
  recorded as failures, so the pass rate was computed over cells that were
  never attempted and read as a quality collapse; the run then finalized as
  `complete`, and resume refuses a complete run, so the skipped cells could
  never be finished. The ceiling now stops the run, marks it `incomplete`,
  says so, and a resume with a higher ceiling finishes it. A cell the ceiling
  skipped leaves no record at all — it was never attempted, so recording it
  would both count it against the pass rate and mark it done for the resume.
  **The CLI exits 1 for an incomplete run** — it did not evaluate what it was
  asked to.

- **A resumed run's totals dropped the first half's judge spend.** Judge calls
  leave no per-cell record, and `finalize` replaces the total rather than
  adding to it, so a resume reported only what its own half spent on judges.

- **Three paths a credential could take out of evaling**, all of them text
  arriving from elsewhere that evaling relayed. A YAML syntax error in a
  secrets file is by construction an error on a line holding a key, and
  PyYAML quotes that line — `evaling doctor` printed it, in the output the
  docs call safe to paste into an issue. The "response was not JSON" branch
  built its error from raw response text without redacting, so a gateway
  reflecting the auth header wrote a live key into `results.jsonl`. And a
  `command` script's stderr — which routinely echoes the key evaling handed
  it — went to disk verbatim.

- **No-look mode leaked through the paths that render without running.**
  `evaling validate` and `run --dry-run` reported raw case ids and render
  errors quoting case content, and the MCP `render_prompt` tool returned fully
  rendered messages with no privacy check at all. Ids are now hashed and
  errors withheld on those paths; `render_prompt` is refused outright, since
  rendering a case *is* reading it and a redacted render answers no question.

- **`--log-requests` could write an API key to disk.** Redaction covered
  values from a secrets file only, so a key from the real environment — the
  normal case — was not scrubbed, and a gateway that echoes the request header
  into its error body put a live credential in the trace. The stored run
  record redacted it correctly all along; the log had no equivalent. Providers
  now register the key they resolved with the log before anything is written.

- **A resumed run could be finished with different filters than it started
  with.** The config fingerprint covers the config and every file it
  references, but not the flags, so `--resume` alongside a different
  `--case`/`--model`/`--variant` ran whatever the new filters selected and
  marked the run complete. With `--sample` it was worse: the draw is by
  position into the filtered list, so resuming over a smaller population
  produced a run whose cells came from two different case sets, with entirely
  ordinary numbers. The matrix a run set out to execute — variant and model
  names, and digests of the cases and the population they were drawn from — is
  now recorded and compared on resume. Identities rather than counts: swapping
  two cases for two others leaves every count unchanged, and the first version
  of this guard let exactly that through.

- `evaling calibrate` on a run with several models paired each rating with an
  arbitrary model's output. Three models produce three answers and the rating
  refers to one of them, so this built a calibration set measuring agreement
  against outputs the rater may never have seen. It now requires `--model`,
  as it already did `--variant`.

- `evaling doctor --check-providers` threw away the whole report when the
  config was broken — on precisely the machine that needed it — and exited 0
  when every provider check failed. Both fixed; a failing check now exits 1.

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
