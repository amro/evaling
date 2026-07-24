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

### `evaling init [--force]`

Scaffold a working example (`eval.yaml`, two prompt variants, a JSONL case
file) that runs offline against the built-in mock provider. Refuses to
overwrite existing files without `--force`.

### `evaling run [CONFIG]`

Run the matrix and print the summary. Exits `0` on success, `1` when the
configured thresholds fail, `2` on config errors.

| Flag | Effect |
|---|---|
| `--model NAME` / `--variant NAME` / `--case ID` | Run a sub-matrix (each repeatable) |
| `--dry-run` | Validate config, render every prompt, print the request count — no model calls. Exits 2 if anything fails to render. |
| `--max-cost USD` | Stop issuing model calls once accumulated cost reaches the limit; remaining cells record a skip error. Under concurrency, overshoot is bounded to roughly one call's cost (one pilot call runs alone until per-call cost is known). |
| `-y, --yes` | Skip the confirmation shown for 100+ request matrices |
| `--no-cache` | Bypass the response cache |
| `--resume RUN` | Finish an interrupted run (same config required; the run keeps its original label — `--label` is ignored) |
| `--baseline RUN` | Gate against this run's aggregates |
| `--label NAME` | Name the run for later reference (`latest` and `baseline` are reserved) |
| `--html PATH` | Also write a self-contained HTML report when the run finishes |
| `--concurrency N` | Max parallel model calls |

With `thresholds.baseline: regression` in the config, the pinned baseline is
used automatically (pin one first with `evaling baseline set`).

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

### `evaling baseline set RUN` / `evaling baseline show`

Pin (or print) the baseline run used by `regression` gating and the
`baseline` run reference.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success (and gate passed, if configured) |
| 1 | Run completed but the threshold gate failed |
| 2 | Config, usage, or reference errors |
