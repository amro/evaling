# CLI reference

```
evaling [GLOBAL FLAGS] COMMAND [ARGS]
```

## Global flags

| Flag | Effect |
|---|---|
| `-c, --config PATH` | Eval config file (default `eval.yaml`) |
| `-o, --output-dir PATH` | Where runs are stored (top of the settings layers) |
| `--cache-dir PATH` | Response cache location |
| `--no-color` | Disable colors (the `NO_COLOR` env var also works) |
| `-q, --quiet` | Errors and essential output only |
| `-v, --verbose` | Per-cell detail during runs and in drill-downs |
| `--json` | Machine-readable JSON on stdout (for scripting) |

Global flags come **before** the command: `evaling --json run`, not
`evaling run --json`.

## Run references

Anywhere a command takes a run, you can pass:

- a **run id** (`20260724T155248123-cb62`)
- a **label** (`--label` from `run`; most recent match wins)
- **`latest`** — the newest run
- **`baseline`** — the pinned baseline run

## Commands

### `evaling init [--force] [--provider NAME]`

Scaffold a working example — `eval.yaml`, two prompt variants, a JSONL case
file, a `.gitignore`, and a `.evaling.secrets.yaml.example` — that runs
offline against the built-in mock provider. Refuses to overwrite existing
files without `--force`.

| Flag | Effect |
|---|---|
| `--provider NAME` | Scaffold for `mock` (default), `anthropic`, `openai`, or `openai-compatible` |
| `--force` | Overwrite existing scaffold files |

`--provider` writes a real model block for that vendor instead of the mock
one, including the environment variable it reads. The scaffolded `.gitignore`
already excludes `.evaling/` and `.evaling.secrets.yaml`; see
[secrets.md](secrets.md).

### `evaling run [CONFIG]`

Run the matrix and print the summary. Exits `0` on success, `1` when the
configured thresholds fail or `--fail-fast` stops the run, `2` on config
errors.

| Flag | Effect |
|---|---|
| `--model NAME` / `--variant NAME` / `--case ID` | Run a sub-matrix (each repeatable) |
| `--sample N` | Evaluate a random subset of up to N cases (see below) |
| `--sample-seed N` | Repeat an earlier draw; requires `--sample` |
| `--dry-run` | Validate config, render every prompt, print the request count — no model calls. Exits 2 if anything fails to render. |
| `--max-cost USD` | Stop the run once accumulated cost reaches the limit. Remaining cells are skipped rather than failed, the run is marked `incomplete` (resume it with a higher ceiling), and the exit code is 1. Under concurrency, overshoot is bounded to roughly one call's cost (one pilot call runs alone until per-call cost is known). |
| `--fail-fast` | Stop at the first failing cell; exits 1 (see below) |
| `--log-requests PATH` | Write a JSONL trace of every provider request and response (see below) |
| `--no-cache` | Bypass the response cache |
| `--resume RUN` | Finish an interrupted run (same config required; the run keeps its original label — `--label` is ignored) |
| `--baseline RUN` | Gate against this run's aggregates (`regression` means the pinned baseline) |
| `--label NAME` | Name the run for later reference (`latest` and `baseline` are reserved) |
| `--html PATH` | Also write a self-contained HTML report when the run finishes |
| `--concurrency N` | Max parallel model calls |
| `--no-look` | Never store or display prompts, outputs, or attachments — scores only ([no-look.md](no-look.md)) |

With `thresholds.baseline: regression` in the config, the pinned baseline is
used automatically (pin one first with `evaling baseline set`).

`--no-look` can turn privacy mode on but never off — a config that sets
`privacy.no_look: true` cannot be loosened from the command line. A run whose
cases come from a source refuses to start without `limit` or `--max-cost`, and
cannot be resumed; see [no-look.md](no-look.md) for why.

#### Debugging a provider

`--log-requests PATH` writes one JSON object per model call: the request body
evaling sent, the response it got, the status, and how long it took. For a
gateway returning something odd, or a `command` script whose stderr you can't
see, it beats adding print statements to evaling and running it from a
checkout.

```sh
evaling run --sample 3 --log-requests trace.jsonl
jq -r 'select(.status >= 400) | .response // .response_text' trace.jsonl
```

The target must be a new file or a previous trace: pointing it at something
else — `--log-requests eval.yaml` is an easy thing to type — is refused rather
than truncated.

**Headers are never written.** The API key travels in a header, so the way to
guarantee the file cannot contain one is to have no code path that writes
them. Values from your secrets file are additionally redacted from the bodies,
for a gateway that reflects credentials back in an error.

A cache hit makes no call, so it writes no entry — pair this with
`--no-cache` or the trace will look like calls that never happened. A retried
call writes one entry per attempt.

The bodies are still your prompts and the model's completions, so treat the
file as you would the run itself. It is refused outright under
[no-look](no-look.md), where a verbatim record is the exact artifact the mode
exists to prevent. Each run truncates the file rather than appending.

#### What a run tells you before it starts

`run` reports the size of the matrix and, where the models are priced, what it
is likely to cost — then runs:

```
Running 4200 requests (2 variants × 3 models × 700 cases)
  estimated ~$38.40
```

**An estimate, not a bound.** Input tokens are approximated from character
counts rather than a real tokenizer; output length is only capped where a
model sets `max_tokens`; the built-in price table is a convenience rather
than an invoice; and retried calls bill again. Treat it as an order of
magnitude for deciding, and `--max-cost` as the thing that actually holds.

LLM judges *are* counted — a judge is a billable call per cell, so a scorecard
with two judged criteria makes three calls per cell.

A model with no pricing is named and left out rather than counted as free, and
if nothing can be priced the line is omitted entirely — `$0.00` would read as
"free" instead of "unknown".

There is no confirmation prompt and no size threshold. Ctrl-C is the escape,
and an interrupted run resumes with `--resume`. For unattended runs
`--max-cost` is the ceiling — it bounds the actual risk rather than asking you
about a proxy for it.

#### Failing fast

`--fail-fast` stops the run as soon as a cell fails, so a broken prompt costs
one cell rather than the whole matrix. Cells already in flight finish and are
recorded — the run finalizes normally and everything that ran is readable with
`evaling show` afterwards.

"Failing" means the cell did not pass — an errored cell counts, including one
skipped by `--max-cost`.

It exits `1` on its own, gate or no gate: a build that stopped early but
exited `0` would read as a pass. The run's summary and `--json` output both
carry `stopped_early`.

A run stopped this way is `complete`, not interrupted, so `--resume` will not
pick it up. Fix the failure and run again.

Best paired with `--sample` for a smoke check before the real matrix:

```sh
evaling run --sample 10 --fail-fast   # does anything work at all?
evaling run --baseline regression     # the run that decides the build
```

#### Sampling

`--sample N` runs a random N of the cases you selected — the fast loop while a
prompt is still moving, instead of listing `--case` ids by hand:

```sh
evaling run --sample 20        # 20 random cases instead of all 2,000
```

It draws **cases, not cells**. The full matrix still runs over each case
drawn, so `--sample 20` against two variants and two models is 80 calls, not
20 — and every cell sees the same 20 cases, which is what keeps the
comparison a comparison.

N is an upper bound: asking for more cases than exist runs all of them rather
than failing, since the flag means "no more than this many" and a dataset that
shrank shouldn't break the command that reads it. `--sample 0` is refused.

`validate` and `--dry-run` sample too, but their draw is illustrative — no
seed is reported, since nothing was spent and nothing was stored. Pass
`--sample-seed` to see a specific one.

Every sampled run reports the seed that produced it, so a draw worth keeping
can be repeated exactly:

```
sampled 20 of 2000 cases — repeat this draw with --sample 20 --sample-seed 2894127714
```

The seed is stored with the run, so `--resume` continues the original draw
rather than making a new one, and `--resume` refuses outright if the filters
changed. `evaling compare` warns when two runs did not cover the same cases —
sampled or not — since otherwise part of every delta is just which cases each
run covered.

Sampling narrows a fixed case list, so it does not apply to
[source-backed runs](large-datasets.md), which have no population to draw
from — use `limit` there. And a sample is a sample: see
[how many cases you need](large-datasets.md#how-many-cases-do-you-need) before
reading much into a small one.

### `evaling validate [CONFIG]`

Check the config and render every prompt without calling any model — the same
work as `run --dry-run`, named so it's findable. Exits `2` if anything fails
to load or render.

| Flag | Effect |
|---|---|
| `--model NAME` / `--variant NAME` / `--case ID` | Validate a sub-matrix (each repeatable) |
| `--sample N` / `--sample-seed N` | Check a random subset, as `run` does |

### `evaling show RUN [--failures] [--case ID]`

Re-render a stored run: the summary matrix by default, failing cells with
reasons via `--failures`, or one case across all variants × models via
`--case` (add `-v` for full untruncated outputs).

### `evaling list [--limit N]`

Stored runs, newest first (default 20).

With `--json`, `list` prints a **bare array** of the full stored metadata for
each run, keyed `id`. The MCP `list_runs` tool returns a different shape —
`{"runs": [...], "total", "baseline"}`, keyed `run_id` — so a script written
against one will not read the other. See [mcp.md](mcp.md#tools).

### `evaling compare RUN_A RUN_B [--html PATH]`

Per variant×model score and pass-rate deltas, with regressions in red, plus
the overall movement. `--html` writes a self-contained comparison page.

### `evaling export RUN --format json|csv|md|html [--out PATH]`

Render a stored run: `json` (full data), `csv` (one row per cell with
per-criterion columns), `md` (paste-into-a-PR summary), `html` (see below).
Writes stdout unless `--out` is given.

### `evaling cache info` / `evaling cache clear`

Inspect or clear the response cache. `info` reports the cache location, entry
count, and total size; `clear` deletes entries.

| Flag | Effect |
|---|---|
| `--older-than DAYS` | Only remove entries older than this (on `clear`) |
| `--yes` | Skip the confirmation prompt (on `clear`) |

The cache key covers only what changes a response — provider, model, base
URL, request parameters, and the rendered messages — so run labels and output
directories never invalidate it. Use `--json` for machine-readable output.

### `evaling calibrate --from-run RUN --labels FILE`

Scaffold an eval that measures how well a judge agrees with you — the recipe
in [evaluating-judges.md](evaluating-judges.md), generated instead of
hand-assembled.

| Flag | Effect |
|---|---|
| `--from-run RUN` | The run whose outputs you rated (required) |
| `--labels FILE` | CSV or JSONL of `case_id` and your rating (required) |
| `--out DIR` | Directory to create (default: `calibration`) |
| `--variant NAME` | Which variant's output to rate, if the run has several |
| `--judge-model ID` | Model the judge will run on |

```sh
evaling run --label to-rate                  # produce answers
# ...rate them in a spreadsheet: case_id,human_label
evaling calibrate --from-run to-rate --labels ratings.csv
cd calibration && evaling validate && evaling run
```

It generates and does not run: no model is called and nothing is spent. The
result is your rated answers as cases, two deliberately different rubric
phrasings as variants, and the `agreement` scorer grading each verdict against
your rating. Read the `close-agreement` row — a judge one point off a human is
useful, a judge three points off is not.

Cases in the run with no rating are left out, and it says how many. Ratings
that match no case in the run are an error rather than an empty file.

### `evaling doctor [--check-providers]`

Report the state of an installation: version, Python, platform, the config it
found, every resolved setting **with the layer that supplied it**, which
secrets file is in play, which API-key variables each model needs and whether
they resolve, and the cache and run-store sizes.

| Flag | Effect |
|---|---|
| `--check-providers` | Also make one tiny call per model to check credentials — the only part that touches the network, and it costs a fraction of a cent |

Exits `1` if it finds problems, so it works as a setup check in a script.
`--json` gives the whole report as one object, which is the useful thing to
attach to a bug report.

The provenance is usually what you came for. When runs are not where you
expect, this says which layer decided:

```
settings  (value, and the layer it came from)
  output_dir   /srv/evals/runs      from EVALING_OUTPUT_DIR
  cache_dir    /home/me/p/.evaling/cache  from default
  concurrency  2                    from config eval.yaml
  cache        True                 from default
```

Secrets are described, never printed: you get the file's path and the names of
the variables it defines, so you can check the name you got wrong without the
value ending up in an issue.

### `evaling mcp`

Start the MCP server on stdio so an agent can drive evaling directly. Needs the
optional extra (`pip install 'evaling[mcp]'`); see [mcp.md](mcp.md).

### `evaling baseline set RUN` / `evaling baseline show`

Pin (or print) the baseline run used by `regression` gating and the
`baseline` run reference.

## HTML reports

`--html` (on `run` and `compare`) and `--format html` (on `export`) produce a
**single self-contained file**: styles are inline, there is no JavaScript at
all, and nothing is fetched from the network — so it opens straight from disk,
survives any Content-Security-Policy, and can be attached to a PR, emailed, or
uploaded as a CI artifact.

It contains the summary matrix, the gate verdict with each check, and a
per-case drill-down where every variant×model cell shows its pass/fail badge,
score, output (or error), the per-criterion breakdown including judge
rationales, and a collapsible view of the exact prompt that was sent. Failing
cases sort first, and a "show failures only" toggle (pure CSS) hides the rest.

Model output is escaped, so a response containing HTML renders as visible text
rather than markup. Binary inputs are referenced by content hash, never
inlined, which keeps reports small.

**Large runs are summarized.** Above 2,000 cells the per-case drill-down is
limited to failing cases and the report says what it omitted. The summary
matrix and gate verdict always cover the whole run. This is a practical limit
rather than a stylistic one: a full drill-down costs roughly 1.5 KB of HTML per
cell, so a 50,000-cell report would be about 75 MB — a file a browser will not
open. For complete per-cell data at that size use `export --format csv`, or
`show --case <id>` for one case.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success (and gate passed, if configured) |
| 1 | The gate failed, `--fail-fast` stopped the run, `--max-cost` left it incomplete, or the run evaluated no cells at all |
| 2 | Config, usage, or reference errors |
