# Changelog

All notable changes to evaling are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.1] - 2026-08-07

Everything here came out of a full-codebase review of the parts earlier
reviews had not reached, and two further rounds verifying those fixes — which
found that the first attempt at the containment fix below covered one of the
two routes, and that a memory note had been "fixed" by deleting the check it
described.

### Security

- **A dataset could name an attachment outside the project.** Attachment paths
  from a dataset are checked to stay under the dataset file, because a dataset
  arrives from elsewhere and evaling reads every attachment, sends it to a
  model API, and archives it with the run. The check applied to relative paths
  only, so writing the same path absolutely skipped it — `file://../../id_rsa`
  was refused and `file:///home/you/.ssh/id_rsa` was not.

  Reaching evaling this way needs a dataset you did not write, a `file://`
  value in it, and a prompt that attaches that field. Present since 0.1.0, and
  no known use of it. An inline case in the config may still use an absolute
  path: that is the config's own author, who can point evaling anywhere
  already, and is the distinction the check was meant to draw.

- **A case variable used as a media path is contained the same way.** Only
  `files` entries were checked, so a prompt templating a plain case variable
  into a media part — `{{ photo }}` rather than `{{ files.photo }}` — resolved
  wherever the dataset said. A literal path written into the config still
  reaches outside, and a `files` attachment is trusted here because it was
  already checked when the case loaded.

- **`--max-cost` rejects a value that is not a finite number.** `spent >= nan`
  is False forever, so a NaN ceiling enforced nothing — while still counting
  as "a ceiling was given", which is what lets an unbounded source start. The
  one flag between a case source and an unbounded bill, switched off by a typo.

- **A malformed secrets file could print the secret it was hiding.** The
  parse error for a secrets file suppresses the offending line by design, but
  still quoted PyYAML's own description — and that description names tokens
  taken from the file. A value beginning with `*` is YAML alias syntax, so
  `MY_KEY: *sk-…` failed with `found undefined alias 'sk-…'`. Quoted spans are
  now dropped from that description; the position and the kind of problem
  remain.

### Fixed

- **A source-backed run stopped early at the first empty page.** Any empty page
  ended iteration, even when its cursor said there was more. Filtering inside a
  source — which [large-datasets.md](docs/large-datasets.md) recommends —
  empties a whole page whenever every row on it is filtered out, so the run
  silently scored a prefix of the data and the gate passed on it. Empty pages
  are now walked through, and a source that returns a thousand in a row while
  still promising more raises rather than appearing to hang. A cursor that
  repeats at any distance is still an error: an `A → B → A` cycle advances at
  every step and never ends, so checking only the previous cursor would walk
  it forever, or quietly fill a `limit` with the same cases.

- **Cases from a source had no ids.** Inline and dataset cases are numbered
  `case-N` when they arrive without one; source cases skipped that entirely, so
  every record carried an empty case id — which grouped unrelated cases under
  one heading in the HTML report and made them indistinguishable in an export.

- **`.nan` and `.inf` were accepted where a number was required.** `params.
  pricing` reached the estimator and produced a NaN cost that failed the cell
  *after* the call was billed — the exact failure that validator exists to
  move earlier. A criterion `weight` of infinity made the weighted score
  `inf/inf`, and the resulting NaN spread into the aggregates, the gate, and a
  `--json` export where NaN is not valid JSON. `timeout_s` accepted infinity,
  which is a request that never gives up. Each is one YAML token away from a
  number.

- **`api_key_env` accepts a variable name, not a key.** It is the one field
  where pasting a credential looks plausible, and the config is serialized
  verbatim into every run's snapshot. Values that are not environment-variable
  shaped are now refused at load.

- **The agreement scorer accepted a tolerance that silently inverted it.** Only
  the type was checked, so a negative tolerance made identical values disagree
  (a perfect judge calibrating at 0%), infinity made everything agree (any
  judge at 100%), and NaN failed every comparison. All three produced a
  plausible number from the tool whose job is measuring agreement. The label
  `"nan"` also compared unequal to itself, since it was normalized to a float.

- **A template expression that failed on its own terms escaped as a bare Python
  error.** `render_text` promises `TemplateError`, but `{{ 1 % q }}` with
  `q: 0` raised `ZeroDivisionError`, naming neither the template nor the case.

- **The HTML report labelled withheld output "(empty response)".** Under
  [no-look](docs/no-look.md) the model answered and the answer was never
  stored, which is a different fact about the run than an empty one.

- **Documented how an API key reaches the MCP server**, which nothing said
  before. An MCP client starts the server with a deliberately minimal
  environment — the reference implementation passes `HOME`, `LOGNAME`, `PATH`,
  `SHELL`, `TERM` and `USER` — so a key exported in your shell never arrives,
  and every cell fails with `no API key found` on a machine where `evaling run`
  works from that same directory. The project secrets file is read by the
  server itself and needs nothing from the client; an `env` block in the client
  config also works. Found by running the server against a real provider.

### Changed

- **CLI/MCP parity is enforced by classification, not by a list of scenarios.**
  Every `evaling run` flag must now be declared shared with its MCP argument,
  or CLI-only with a reason, so a new option on either surface fails the tests
  until someone places it. The scenario table only ever covered options it
  happened to name, which is why `--fail-fast`, `--no-cache`, `--resume`,
  `--baseline` and `--no-look` sat in neither it nor the deliberate-differences
  list. The options both surfaces share are also checked to *behave* the same
  — `--max-cost` had been tested only as the thing that lets a run start, never
  as a ceiling — and refusals must now agree on the reason, not just refuse
  together.

## [0.2.0] - 2026-08-07

**Breaking.** Two contracts moved, both around the optional MCP extra. It now
requires `mcp` 2.0 or newer, so an environment pinned to 1.x cannot install
it; and `evaling doctor`'s `mcp_extra` JSON field keeps its name and boolean
type but now means *usable* rather than *present*. Nothing about the config
format changed, and the CLI is untouched — a project that does not use the MCP
server is unaffected. Both are detailed below.

### Changed

- **The MCP server needs `mcp` 2.0 or newer.** The 2.0 SDK renamed `FastMCP`
  to `MCPServer` and moved it out of `mcp.server.fastmcp`, so the import the
  server is built on no longer resolved and `evaling mcp` could not start at
  all. Since the extra's floor was `mcp>=1.28`, a fresh
  `pip install 'evaling[mcp]'` resolved to 2.0 and got a server that was dead
  on arrival — so this is a fix for new installs as much as a version bump.
  The requirement is now `mcp>=2.0,<3`; 1.x is upstream maintenance-only, and
  the two lines cannot be served from one import path.

  The upper bound is deliberate. Refusing arguments a tool never declared
  needs the SDK's tool-manager internals, so a major SDK release is entitled
  to break it — and it fails shut, refusing every call that carries an
  argument, which is a broken server rather than a silent one. Capping means
  the next major arrives as a dependency PR with CI attached instead of as a
  user's outage.

  The migration retired two private-API dependencies rather than porting
  them. The server's version is a constructor argument now, instead of being
  poked onto `_mcp_server` after the fact, and the in-process tests connect
  with the public `Client(server)` rather than reaching through the server for
  a lowlevel handle. Unknown arguments are still refused, still by name only
  (see [mcp.md](docs/mcp.md#argument-names-must-match-exactly)) — the SDK drops
  what a tool didn't declare in 2.0 exactly as it did in 1.x.

- **`evaling doctor` reports whether the MCP extra is *usable*, not merely
  present**, and names its version. It answered `find_spec("mcp") is not None`,
  so a 1.x install — the one case where `evaling mcp` refuses to start — read
  as a clean bill of health in the one command built for "why is this not
  working". The JSON field `mcp_extra` keeps its name and boolean type but now
  means usable; `mcp_version` is new alongside it.

### Fixed

- **An `mcp` that is installed but unusable no longer reads as one that is
  missing.** Absent, too old, and installed-but-broken all arrive as an
  `ImportError` from the same line, and the handler blamed the one it could
  name: someone on 1.x was told to install the package they already had. The
  version is now read from distribution metadata rather than by importing —
  importing to find out is precisely what makes a broken install look like an
  absent one — and each case gets its own message. The one case that cannot be
  diagnosed from here, a 2.x that fails to import, hands back the underlying
  error instead of guessing, and the `ImportError` is chained rather than
  suppressed so the traceback still names the module that actually failed.

- **A judge stopped by `--max-cost` no longer poisons the cell it was grading.**
  When the ceiling was crossed after a cell's own call landed but before its
  judge could start, the judge's criterion was scored `0` and recorded with
  `skipped: max cost limit reached`. That contradicted the rule the ceiling is
  built on — a cell it stops is *owed*, not failed, so it leaves no record —
  and the consequences were permanent: the cell counted as a quality failure
  in the aggregates and gated the run, and because it now had a record, a
  resume with a higher ceiling skipped it and the judge never ran. Under the
  default concurrency every cell in flight at the crossing point was affected,
  not just one. The cell is now dropped and stays owed.

- **A resumed run counts what it already spent on judges.** `--max-cost` seeded
  its ledger from prior cells only, so each resume began afresh as far as judge
  spend was concerned: a heavily judged eval stopped and resumed three times
  cost three ceilings. The reported judge total was already cumulative; only
  the enforcement was not.

- **Five text models added to the price table**, checked against all three
  published rate cards on 2026-08-07. No existing rate changed. New: OpenAI's
  `gpt-5.3-chat-latest`, `gpt-5.3-codex`, `gpt-5.2-chat-latest`, and
  `gpt-5-search-api`, and Google's `gemini-3-flash-preview`. Lookup is by exact
  id, so a variant priced the same as its base model still needs its own entry
  — without one it reports an *unknown* cost, which is worse than a stale one.

- **`docs/cli.md` documented a `-v` that the CLI refuses.** It showed
  `evaling show <run> --case <id> -v`; `-v` is a global option and has to
  precede the subcommand, so anyone who copied that line got
  `Error: No such option '-v'`. The feature always worked — only the
  documented spelling was wrong. Corrected here and in 0.1.1's entry above,
  where the same line shipped.

  The docs tests checked that every command and flag *exists*, which cannot
  catch a real flag in a position click rejects. They now also parse every
  documented `evaling …` line, running none of them.

- **Documentation corrections found by auditing every claim against the code.**
  `hash_case_id`'s worked example printed a digest belonging to a different id
  (and used an email address as its sample id, which the repo does not carry
  anywhere else). `dry_run` was said to raise on the first template problem; it
  collects them per cell in `report.errors` and raises only for config
  problems. A cell skipped by `--max-cost` was described as a failing cell.
  Three files claimed a source-backed run without a `limit` refuses to start
  unconditionally — it refuses when nothing is watching, and `examples/no-look`
  additionally offered a `--yes` flag that does not exist. `secrets.md` said a
  missing API key fails the run before any request; every cell on that model
  fails and the run finishes. The `calibrate` flag table omitted `--model`, the
  large-report note omitted that failing cases are themselves capped at 200,
  `configuration.md`'s typed-part list omitted `video`, and the tutorial called
  its own five-block example four.

- **`evaling doctor` no longer calls an `mcp` past the cap usable.** The extra
  installs `mcp>=2.0,<3`, but doctor accepted any 2.0-or-newer, so a
  force-installed 3.x read as healthy — and the line describing it said "too
  old".

### Security

- **`cryptography` 50.0.0** (CVE-2026-69247) — a Bleichenbacher timing oracle
  in PKCS#7 decryption. It reaches evaling only through the `mcp` extra, which
  depends on `pyjwt[crypto]` unconditionally, on a code path nothing in evaling
  calls; no released version was exploitable through it.

## [0.1.1] - 2026-08-01

### Added

- **`-v` prints each cell's prompt, response, and scores as it finishes.** It
  previously printed one line per cell — the response, flattened and cut at 60
  characters, with no indication of whether the cell passed. The rendered
  prompt had no human-readable surface anywhere: it is stored on every record
  and was reachable only through `export --format json`, which is a strange
  place to send someone debugging a template. `evaling -v show <run> --case
  <id>` prints the same block from storage.

  Bounded at 20 lines per prompt message and 200 lines of response, with a
  character backstop, since a response with no newlines is one line however
  long it is. The prompt is capped tighter because it repeats on every cell,
  while the response is the reason to turn the flag on. Nothing prints under
  `--quiet` or `--json`. Under [no-look](docs/no-look.md) there is nothing to
  print and the block says so.

### Changed

- **`evaling init` scaffolds one eval instead of three demos.** The cases were
  three unrelated tasks whose `expected` values were words lifted from their
  own questions — "What is the capital of France?" expected "France", where
  the answer is Paris. That passed, and taught the reader that `expected` need
  not be the answer. It is now one task, triaging flagged card transactions,
  and the criterion is whether the model cited the detail that should decide
  the case. Each `expected` is quoted from its own transaction and appears in
  no other, so swapping two of them drops the run from 100% to 33%; the old
  cases stayed at 100% no matter what `expected` said.

  Case ids changed (`midnight-watch`, `weekly-grocery`, `retry-burst`), and
  the case variable is now `transaction` rather than `question`. This affects
  newly scaffolded projects only.

- **Cost estimates now cover OpenAI and Gemini models, not just Claude ones.**
  `evaling init --provider openai` scaffolds `gpt-5.2`, which had no price, so
  a user who took evaling's own suggestion got no cost estimate at all — the
  line is omitted entirely rather than showing `$0.00`. The table now carries
  the published first-party text-model rates for Anthropic, OpenAI, and Google
  Gemini, and a test covers every model a scaffold writes into a config.

  Endpoints whose operator sets the rates — Ollama, vLLM, OpenRouter, an
  internal gateway — still report no cost, because no table can know them.
  `params.pricing` remains the answer there, and now also for Gemini Pro evals
  whose prompts exceed the short-prompt tier the table carries.

- **The Claude rates match Anthropic's published rate card**, which added
  Opus 4.5, Sonnet 4.5, and the deprecated and retired models — still callable
  on the cloud platforms, and a model missing from the table reports an
  unknown cost rather than a stale one. Sonnet 5 carries its standard rate
  rather than the introductory pricing ending 2026-08-31, so its estimate
  reads high until then; the newest models tokenize to roughly 30% more tokens
  than the estimator's four-characters-per-token assumption, so their input
  half reads low. Both are documented, and `params.pricing` overrides either.

- **`--sample` is documented as drawing cases, not cells.** The tutorial said
  it "runs a random subset instead of the whole matrix", which reads as a cap
  on cells; the whole matrix still runs over each case drawn, so `--sample 20`
  against two variants and two models is 80 calls. That it takes all cases
  when asked for more than exist was documented nowhere.

### Fixed

- **LLM judge calls now use the response cache.** They went straight to the
  provider, which made the cache's promise backwards: rerunning a plain eval
  was free, while rerunning a *judged* one — at two or three calls per cell —
  still paid in full for every judgment. A judge sees the output and the
  rubric, both covered by the key, so an edited rubric still misses. Cached
  judgments add nothing to `judge_cost_usd`.

- **A credential in a judge's reply could reach the cache on disk.** Judge
  completions were not scrubbed before being returned, and the cache stores
  the completion, so redacting what a record carries happens too late. The
  cell path had this right and the judge path had not inherited it.

- **A run that used the cache says so.** "My second run finishes immediately"
  was reported as a bug; it was the cache working, but the only sign of it was
  one word inside a dense totals line on a run that was over before anyone
  read it. A fully cached run now says no model was called, and names
  `--no-cache` — unless a judge ran anyway, which an edited rubric causes, in
  which case it says only the judges were called rather than contradicting the
  cost printed directly above it.

  `totals` gains `judge_calls` to make that distinction possible: an unpriced
  judge model bills nothing and is still a call, so `judge_cost_usd` cannot
  answer "did anything run". Like `judge_cost_usd` it is cumulative across a
  `--resume`.

## [0.1.0] - 2026-07-31

First release. The entries below are the development history leading to it —
`Changed` and `Fixed` describe work against unreleased code, since there was
no previous version to change or to have bugs in. They are kept because how a
thing came to work is often the most useful documentation of why it works
that way.

### Changed

- **The large-matrix confirmation is gone; runs report what they will cost
  instead.** `run` now prints the matrix size and, where the models are
  priced, the likely spend — `estimated ~$38.40` — and then runs. `--dry-run`
  and `validate` show the same line. Deliberately hedged: token counts are
  approximated, the price table is a convenience rather than an invoice, and
  retries bill again, so a confident-looking figure would be the wrong kind
  of help. `--max-cost` is what actually holds. LLM judges *are* counted: a
  judge is a billable call per cell, so a scorecard with two judged criteria
  makes three calls per cell, and leaving them out understated a judged run
  by roughly half.

  The prompt fired on a fixed cell count, which is a poor proxy for cost: a
  hundred cells against a local model is free, and anyone whose ordinary eval
  is larger than the threshold learned to paste `-y`. A guard bypassed
  reflexively is not protecting anyone, and this one had grown a second,
  undocumented meaning — `--yes` also waived the unbounded-source refusal, so
  everyone who added it to stop prompts had silently authorized unbounded
  spend.

  **Removed:** `--yes` on `run` (`cache clear` keeps its own), `confirm_large`
  over MCP, and the threshold itself. Ctrl-C is the escape and an interrupted
  run resumes; `--max-cost` remains the ceiling for unattended runs, bounding
  the real risk rather than a proxy for it.

  One refusal survives: an unbounded source with no `--max-cost` **and no
  terminal attached** — CI or an agent — where nothing can interrupt the run
  and the size is unknown even to whoever wrote the source. At a terminal it
  now just runs.

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

- **Mutation testing** (`[tool.mutmut]` in `pyproject.toml`, weekly CI job).
  It changes the source and checks whether a test notices — which is the
  automated form of the review finding that keeps recurring here: a test that
  passes with the code it guards deleted.

  Scoped to the five modules that encode invariants, and to the tests that
  could kill mutants in them; mutmut resolves its own relative paths inside
  each test, so any test that changes directory breaks the run. 714 mutants,
  82% killed, about a minute. Report-only, not a gate: a surviving mutant is a
  question, since some are equivalent and some survive only because the
  narrowed selection excluded their killer.

  Its first real run found that `scrub_secrets` was untested — three separate
  mutations of the score-detail loop survived the whole suite. A judge's call
  bypasses the cell path, so its rationale is never scrubbed on the way to the
  cache, making that function the only thing between a credential in a judge
  rationale and disk. Now covered.

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

- **A full mutation-testing triage, and what it found.** Every surviving
  mutant in the modules that encode invariants was classified. The genuine
  gaps, now covered: `load_secrets` stopped searching at the first *missing*
  candidate file, so a project without `.evaling.secrets.yaml` would never
  have read `~/.config/evaling/secrets.yaml` — nothing walked past a missing
  candidate; the input-token half of the cost estimate could contribute
  nothing without a test noticing; `scrub_secrets` was exercised only through
  full runs where the engine had already redacted the text, leaving the
  credential path itself unexercised; the request log's serialization
  fallback was marked unreachable and was not (a circular structure reaches
  it); a one-byte file and a non-UTF-8 file at the `--log-requests` target;
  the eight-character floor below which a value is too short to redact; the
  reported line and column of a YAML error; a BOM at the start of a file.

  No behaviour changed — these were all missing tests rather than broken
  code. Survivors: 135 → 77, all of them now in the equivalence classes
  documented in `CONTRIBUTING.md`.

- **A run that evaluated nothing announced a quality regression.** Aggregates
  report `pass_rate: 0.0` when no cell ran — a "no data" fallback — and a
  threshold read it as "0% of cases passed" and failed the gate. So a
  `--max-cost` ceiling reached before the first call printed
  `gate FAILED — pass rate 0.00% vs required 50.00%`, which is a claim about
  quality that nothing had measured, in the same output as a warning saying
  the scores covered only what was attempted.

  Thresholds are still evaluated over whatever cells ran; zero cells now
  yields no verdict, reported as `gate not evaluated — no cell ran`, and
  `gate` is `null` in `--json`. "No verdict" is not "passed": a run that
  evaluated nothing exits `1` whether or not thresholds were configured, so
  an empty case source or an exhausted budget cannot take a build green. What
  changed is the reason CI is given — `no cell ran`, or `incomplete: cost
  ceiling` — rather than a phantom regression.

- **`--label latest` and `--label baseline` were refused after the run
  appeared to start.** Both are reserved because `resolve_ref` resolves them
  before labels, so a run carrying one would be shadowed and unreachable by
  name. The check lived only in `create_run`, which the engine reaches after
  the CLI has printed `Running N requests` and painted a progress bar —
  nothing was spent and no run directory was left, but it read as a crash
  rather than a refusal. The CLI now checks before the run starts, beside the
  existing `--log-requests` pre-flight; the store keeps its own check for
  library and MCP callers.

- **The test suite could reach the network and spend a real API key.**
  "Tests never touch the network, no real API keys" was a rule in
  `CONTRIBUTING.md` and nothing else, and it was already being broken: one
  test ran a full eval against an `openai-compatible` model and genuinely
  resolved and connected to the host in its `base_url`. Nothing removed
  credentials from the environment either, so on a machine with
  `ANTHROPIC_API_KEY` exported — evaling's own primary variable, so most
  contributors' machines — a test of that shape would have spent that key
  against the live API.

  `tests/conftest.py` now enforces both: every `*_API_KEY` variable and
  `EVALING_SECRETS` are stripped for the whole suite, and an HTTP client
  built without an injected transport gets one that refuses to send, naming
  the host and how to stage the call. `tests/test_suite_isolation.py` fails
  if either guard is removed. Contributors are unaffected — the suite passes
  unchanged — but a test that quietly starts making real calls no longer can.

  This also made the suite safe to run under `fork()`: consulting the macOS
  proxy resolver in a forked child was segfaulting 41 of the mutation-testing
  runs, which had been reporting as inconclusive rather than as failures.

- `evaling --json show <run> --failures` ignored `--failures` and returned
  every record, with nothing to say it had not been narrowed.
- `totals.cost_usd` counted cached cells, so a re-run served entirely from
  cache reported the full price of the original — and a resumed run seeded its
  `--max-cost` budget with money it had never spent. The per-cell record still
  carries what the call would have cost.
- A judge answering `true` scored a perfect 1.0 (`bool` passes
  `isinstance(_, Real)`), and `NaN` survived the clamp because every
  comparison with it is False. Both turned a broken judge into a passing
  result; both now fail the criterion.
- A non-numeric `scale`, `pass_at`, or `tolerance` produced a traceback rather
  than a message — `ScorerSpec` allows extra keys, so the schema accepts
  anything there.
- A UTF-8 byte-order mark, which Excel writes and nobody sees, made a CSV's
  first column `\ufeffquestion` so `{{ question }}` failed as undefined.
- Markdown export interpolated labels, variant, model, and case names without
  escaping, so a `|` in any of them broke the table.
- `evaling list` crashed on a `run.json` that parsed but was not a run
  (`null`, a list, a mapping with no id), taking every other run with it.
- `evaling export --help` did not mention `html`, which it accepts.

- **A dataset could name a file outside its own directory.** evaling reads
  every attachment, hashes it, sends it to a model API, and archives it in the
  run directory — so `file://../../../.ssh/id_rsa` in a CSV from a vendor or a
  colleague did all four. A relative attachment path must now stay under the
  file that declared it; absolute paths are unaffected, since those are a
  deliberate choice rather than something a dataset can smuggle in.

- **`--log-requests` truncated whatever it was pointed at.** Each run starts a
  fresh trace, so `--log-requests eval.yaml` destroyed the config. It now
  refuses any target that already has content and does not look like a trace.

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
