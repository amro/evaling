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
configured thresholds fail, `2` on config errors.

| Flag | Effect |
|---|---|
| `--model NAME` / `--variant NAME` / `--case ID` | Run a sub-matrix (each repeatable) |
| `--sample N` | Evaluate a random N of the selected cases (see below) |
| `--sample-seed N` | Repeat an earlier draw; requires `--sample` |
| `--dry-run` | Validate config, render every prompt, print the request count — no model calls. Exits 2 if anything fails to render. |
| `--max-cost USD` | Stop issuing model calls once accumulated cost reaches the limit; remaining cells record a skip error. Under concurrency, overshoot is bounded to roughly one call's cost (one pilot call runs alone until per-call cost is known). |
| `-y, --yes` | Skip the confirmation shown for 100+ request matrices |
| `--no-cache` | Bypass the response cache |
| `--resume RUN` | Finish an interrupted run (same config required; the run keeps its original label — `--label` is ignored) |
| `--baseline RUN` | Gate against this run's aggregates |
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

#### Sampling

`--sample N` runs a random N of the cases you selected — the fast loop while a
prompt is still moving, instead of listing `--case` ids by hand:

```sh
evaling run --sample 20        # 20 random cases instead of all 2,000
```

Every sampled run reports the seed that produced it, so a draw worth keeping
can be repeated exactly:

```
sampled 20 of 2000 cases — repeat this draw with --sample 20 --sample-seed 2894127714
```

The seed is stored with the run, so `--resume` continues the original draw
rather than making a new one. It also means comparing two sampled runs is only
meaningful when they share a seed — otherwise you are comparing different
cases, not different prompts.

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
| 1 | Run completed but the threshold gate failed |
| 2 | Config, usage, or reference errors |
