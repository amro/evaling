---
name: running-evals
description: This skill should be used when the user asks to "set up an eval", "compare prompt variants", "test two prompts", "check if this prompt is better", "score model outputs", "add an eval to CI", or otherwise wants to measure whether a prompt or model change is an improvement. Covers authoring eval.yaml, choosing scorers, running offline without API keys, and gating on regressions with evaling.
version: 0.1.0
---

# Running evals with evaling

evaling compares prompt variants and models over a set of cases. It runs a
matrix — every variant against every model against every case — scores each
cell, and reports per-variant results. A run is stored on disk so later runs
can be compared against it.

The tool is reached two ways, and both are equivalent:

- **MCP tools**, if the server is connected: `run_eval`, `get_run`,
  `get_case_result`, `compare_runs`, `list_runs`, `set_baseline`,
  `render_prompt`.
- **The CLI**: `evaling run`, `evaling show`, `evaling compare`, `evaling list`,
  `evaling baseline set`, `evaling validate`.

Prefer the MCP tools when they are available — they return structured results
rather than rendered tables. Fall back to the CLI otherwise.

## The loop

A single run is a measurement, not an answer. The discipline that makes evaling
useful is the comparison:

1. **Establish a baseline.** Run the current prompt, then pin it:
   `set_baseline` (or `evaling baseline set <run-id>`).
2. **Change one thing.** A variant's prompt, a model, a parameter — one, so the
   result attributes to something.
3. **Run again**, then **`compare_runs`** (`evaling compare`) against the baseline.
   Read which cases moved, not just the aggregate: a score that holds steady
   while half the cases flip in each direction is not a stable prompt.
4. **Re-pin only on a real improvement.** The baseline is the claim you are
   defending; move it deliberately.

`thresholds` in the config turns this into a gate — `evaling run` exits
non-zero below `min_pass_rate` or `min_score`, and `thresholds.baseline` gates
against the pinned run rather than an absolute number. That is what belongs in
CI.

## Authoring eval.yaml

`evaling init` scaffolds a project with a commented config, a `.gitignore`, and
a secrets example. Start there rather than from a blank file.

For the complete set of legal keys, providers, scorers and their parameters,
read `references/config-reference.md`. The config rejects unknown keys, so a
typo fails at load time rather than silently doing nothing.

When the MCP server is connected, its `evaling://config-schema` resource is the
same information generated from the installed evaling. It is authoritative for
key names, types and enums. It cannot express the loader's cross-field rules —
a provider's required companion field, unique ids, an `llm-judge` naming a
judge that exists — so this reference covers those and a config matching the
schema can still be rejected. `evaling validate` settles it either way.

Two facts worth knowing before writing a config, because they are the ones most
often guessed wrong:

- **Comparison scorers default to the case's `expected` field.** Omit `value`
  on `exact`/`contains`/`not-contains` and each case is compared against its
  own expected answer. Setting `value` compares every case against one
  constant, which is rarely what is wanted.
- **Relative paths resolve against the config file's directory**, not the
  working directory. That covers prompt files, case files, Python scorers, and
  the `command` provider's executable.

Run `evaling validate` before `evaling run`. It loads the config, renders every
prompt, and reports how many requests a run would make — without calling a
model or spending anything.

## Running without API keys

Ordinary runs call a real provider and cost money. Two setups avoid that, and
they are not interchangeable:

- **`provider: mock`** returns a fixed string, or by default echoes the
  rendered last user message — so its output does vary with the case, but only
  by repeating the prompt back. Use it to check that a config loads and prompts
  render. It is not reasoning about anything, so scores from a mock run say
  nothing about whether a prompt is any good.
- **`provider: command`** shells out to a local program: evaling writes the
  rendered request to its stdin as JSON and reads the completion from its
  stdout. This is the way to run a real, differentiated eval offline — against
  a local model, a stub, or any script.

The `command` contract, in full:

- `command` is a single shell string (`python3 classify.py`), not a list.
- It runs with the working directory set to the config file's directory.
- Stdin is a JSON request whose `messages[]` each carry `role` and `parts[]`.
  Every part has a `type`: a text part is `{"type": "text", "text": "..."}`.
- Stdout is the completion text. A non-zero exit is reported as a cell error.
- The default timeout is 300s; set `timeout_s` on the model to change it.

## Cost control

Runs bill per cell, and a matrix multiplies. Before a first real run, use
`evaling validate` for the request count, and pass `--max-cost` to `run` to
cap spend — the run stops rather than overspending. `--sample N` runs a subset
of cases while iterating on a config.

## Reading results

`get_run` returns a run's summary; pass `detail` for per-cell rows.
`get_case_result` returns one cell in full, including the prompt that was sent
and the output that came back — that is the tool for "why did this case fail",
and it beats guessing from an aggregate score.

When a variant regresses, look at individual failing cases before rewriting the
prompt. The failure is often in the cases, not the prompt: an `expected` value
that admits more than one right answer, or a scorer stricter than the task.

## Common mistakes

- Writing one variant and calling it an eval. Two variants is the minimum for a
  comparison to mean anything.
- Reading only the aggregate score. `compare_runs` reports which cases moved.
- Treating a small case set as conclusive. Six cases will not separate two
  close prompts; the difference has to exceed the noise.
- Using `mock` for anything except a config smoke test.
- Putting API keys in `eval.yaml`. evaling refuses to read them from config at
  all — use the environment or `.evaling.secrets.yaml`, which `evaling init`
  gitignores. An MCP server does not inherit your shell environment, so the
  secrets file is usually the right answer when running through Claude Code.
