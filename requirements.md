# evaling — Requirements

**Status:** settled for v1. This document reflects the design decisions made
2026-07-24; changes from here should go through discussion + a doc update first.

## 1. Overview

`evaling` is an open-source command-line tool for evaluating LLM prompts. Its core
job: run one or more **prompt variants** against one or more **models** over a set
of **test cases**, score the outputs against a **scorecard**, and present a clear
comparison so the user can decide which prompt/model combination to ship.

### Goals

- Make A/B-comparing prompt variants and models a one-command operation.
- Terminal-first UX: readable tables/matrices, sensible defaults, no required web UI.
- Reproducible runs: results are stored and can be re-rendered, diffed, and shared.
- Extensible scoring: built-in checks, LLM judges, and user-defined Python scorers.
- Usable by humans (CLI, HTML reports) and by agents/programs (Python API, MCP server).
- Robust automated test suite from day one; no provider network calls in tests.

### Non-goals (v1)

- Not a hosted service, dashboard, or team collaboration platform.
- Not a prompt-management/versioning system (users keep prompts in their own repo).
- Not a tracing/observability tool for production traffic.
- No fine-tuning or training features.

## 2. Architecture

Three layers, strictly ordered:

1. **Core library** (`evaling` Python package) — config loading, templating, the
   run engine, providers, scorers, storage, exports. Public, documented API:
   everything the CLI can do is doable programmatically.
2. **CLI** — a thin wrapper over the core library (argument parsing + rendering only).
3. **MCP server** (`evaling --mcp`) — a second thin wrapper over the same core,
   exposing eval operations as MCP tools.

No feature may be implemented in the CLI or MCP layer if it belongs in the core.

## 3. Core concepts

| Concept | Description |
|---|---|
| **Eval config** | A YAML file describing an eval: prompts, models, test cases, scorecard. |
| **Prompt variant** | A named prompt template: an ordered list of messages (multi-turn supported), each with typed content parts. Text parts are Jinja2 templates. |
| **Model** | A provider + model id + parameters (temperature, max tokens, etc.). |
| **Test case** | Input variables and file attachments for the template, plus optional expected output, reference data, and `human_label` for scoring/calibration. |
| **Scorer** | A function that grades a model output for a test case, producing a score and pass/fail. |
| **Scorecard** | A named set of weighted criteria, each backed by a scorer; aggregates to per-cell and per-run scores. |
| **Run** | One execution of the full matrix: variants × models × test cases. Persisted on disk with outputs, scores, timing, token usage, and cost. |

## 4. Functional requirements

### 4.1 Configuration

- Eval configs are **YAML** (`eval.yaml` by default).
- Prompts can be defined inline or referenced as external files
  (e.g. `prompts/summarize-v2.yaml`).
- Text content is templated with **Jinja2** (variables, conditionals, loops),
  rendered with strict-undefined so typos in variable names fail loudly.
- Test cases can be listed inline or loaded from CSV/JSONL files.
- API keys come from environment variables (e.g. `ANTHROPIC_API_KEY`), never from
  config files.

**Eval definition vs workspace settings.** The eval config (models, variants,
cases, scorecard, judges, thresholds) is portable and version-controlled: anyone
who clones the repo gets the same eval. Workspace settings (output directory,
cache directory, concurrency, cache on/off) are machine/user concerns and resolve
in layers, most specific wins:

1. CLI flags (e.g. `--output-dir`)
2. Environment variables (`EVALING_OUTPUT_DIR`, `EVALING_CACHE_DIR`,
   `EVALING_CONCURRENCY`, …)
3. `settings:` block in the eval config (shareable project defaults)
4. User config at `~/.config/evaling/config.yaml`
5. Built-in defaults (runs in `.evaling/runs/`, cache in `.evaling/cache/`)

### 4.2 Messages and multimodal inputs

- Prompts are **multi-turn**: an ordered list of `{role, content}` messages
  (system/user/assistant). Single-turn is just the one-message case.
- Message content is a list of **typed content parts**: `text`, `image`, `file`
  (PDF/documents), `audio`. Text parts go through Jinja2; binary parts are
  declared references, never inlined in templates:

  ```yaml
  cases:
    - vars: {question: "What breed is this dog?"}
      files: {photo: ./fixtures/dog1.jpg}
  prompt:
    - role: user
      content:
        - text: "{{ question }}"
        - image: "{{ files.photo }}"
  ```

- **v1 media types: images, PDFs, and audio.** Provider adapters translate parts
  to each API's native format and must fail with a clear error when a provider or
  model doesn't support a part type (e.g. audio on a text-only model) — at config
  validation time where possible, not mid-run.
- In CSV/JSONL datasets, a `file://` value marks a column as a file reference.
- Binary files are hashed by content: cache keys include the content hash, and
  run storage keeps binaries content-addressed so repeated runs don't duplicate them.

### 4.3 Providers and model calling

- Models are called via **direct provider APIs** through a small, pluggable
  `Provider` interface. v1 providers:
  - **Anthropic** (first-class)
  - **OpenAI** (first-class)
  - **OpenAI-compatible** (base URL + key: covers Ollama, vLLM, OpenRouter, and
    most hosted/local backends)
  - **`command`** — shell out to any CLI or script (e.g. `claude -p`, an agent
    harness): request on stdin, response on stdout. Lets evaling evaluate
    anything, not just chat APIs.
  - **Mock** — deterministic fake provider; ships in the package (used by the
    test suite and available to users for dry runs).
- MCP is **not** a model-calling transport in v1 (it is not an inference
  protocol). The pluggable provider interface must, however, allow future
  transports (MCP sampling, arbitrary HTTP) without engine changes.
- Per-model parameters: temperature, max tokens, system prompt override, etc.
- Rate limiting and retry with backoff, configurable per provider.
- Concurrency: requests run in parallel with a configurable limit.

### 4.4 Scoring, scorecards, and autoraters

**Built-in scorers (v1):**

- `exact` — output equals expected value.
- `contains` / `not-contains` — substring checks (case-sensitivity configurable).
- `regex` — output matches a pattern.
- `json-valid` / `json-schema` — output parses as JSON / matches a schema.
- `llm-judge` — an autorater grades the output (see below).
- `python` — user-supplied Python function for custom scoring.
- **Agreement scorers** for judge calibration: exact agreement, within-N,
  correlation / Cohen's kappa against `human_label`.

**Scorecard:** users define quality as named, weighted criteria, each backed by a
scorer:

```yaml
scorecard:
  - criterion: accuracy
    weight: 3
    scorer: {type: llm-judge, judge: quality-judge}
  - criterion: format
    weight: 1
    scorer: {type: json-schema, schema: ./schemas/answer.json}
```

Per case: per-criterion scores and pass/fail. Per run: weighted aggregate per
variant × model cell, which feeds thresholds (4.7).

**Autorater (`llm-judge`):** a judge is a first-class prompt — a template + judge
model + rubric, with a **structured output schema** (score + rationale) enforced
via the provider's structured-output support so parsing never flakes. Judge
definitions live in config and are reusable across criteria.

**Evaluating the autorater (meta-evals):** because a judge is just a
prompt + model, evaling evaluates judges with its own machinery. Test cases carry
an optional `human_label`; users run judge-prompt variants over a calibration set
of (output, human label) pairs and use the agreement scorers to find which rubric
best matches human judgment. Requirements: the `human_label` field, the agreement
scorers, and a documented recipe/example in the repo.

### 4.5 CLI

**Global flags** (all commands): `-c/--config PATH`, `-o/--output-dir PATH`,
`--cache-dir PATH`, `--no-color`, `-q/--quiet`, `-v/--verbose`, `--json`
(machine-readable stdout for scripting).

**Commands:**

- `evaling init` — scaffold an example `eval.yaml` and directory layout.
- `evaling run [CONFIG]` — run the eval matrix; stream progress; print summary.
  - Matrix filtering: `--model NAME`, `--variant NAME`, `--case ID` — each
    repeatable, to run any sub-matrix.
  - `--dry-run` — validate config, render all prompts, print request count and
    cost estimate; makes no model calls. (Doubles as a CI lint for eval configs.)
  - `--max-cost USD` and `-y/--yes` (skip the large-matrix confirmation).
  - `--no-cache` (bypass cache), `--resume RUN_ID` (continue an interrupted run).
  - `--baseline RUN_ID` (override the regression-gate baseline).
  - `--label NAME` — human-friendly run name.
  - `--html PATH` — write the HTML report at the end of the run.
  - `--concurrency N`.
- `evaling show <run> [--failures] [--case ID]` — re-render a stored run.
- `evaling compare <run-a> <run-b> [--html PATH]` — diff two runs.
- `evaling list [--limit N]` — list stored runs.
- `evaling export <run> --format json|csv|md|html [--out PATH]` — render a stored run (4.8).
- `evaling baseline set <run>` — pin the blessed baseline run.
- `evaling mcp` — start the MCP server (4.6).

**Run references:** anywhere a command takes a run, accept: full run id
(timestamp-sortable, e.g. `2026-07-24T1530-a1b2`), a `--label` name, `latest`,
or `baseline` (the pinned baseline).

UX requirements:

- Summary view: matrix of variants × models with aggregate scorecard scores,
  cost, latency.
- Detail view: drill into a single case's output(s) side by side.
- Exit code reflects pass/fail thresholds (4.7) so `evaling run` works as a CI gate.
- Show estimated request count before a run; `--max-cost` guard for large matrices.
- Respect `NO_COLOR`; degrade gracefully in non-TTY environments.

### 4.6 MCP server mode

- `evaling mcp` starts an MCP server (stdio) exposing core operations as tools.
- Primary use case: **agent-driven prompt iteration** — an MCP client
  (e.g. Claude Code) tweaks a prompt, runs the eval, reads scores, and iterates.
- CI is explicitly *not* the target for MCP mode; CI uses the CLI
  (exit codes + JSON/HTML exports).
- Design principle: **the consumer is an LLM — responses must be token-frugal.**
  Summaries by default; drill-down on demand; pagination on anything unbounded.
- Tools (v1):
  - `run_eval(config_path, models?, variants?, cases?, label?, no_cache?, max_cost?)`
    — **blocking**: runs to completion, emitting MCP progress notifications, and
    returns the summary (run id, aggregate matrix, failure count, cost).
  - `get_run(run_id, detail=summary|failures|full, filters?, page?)`.
  - `get_case_result(run_id, variant, model, case_id)` — full detail for one cell.
  - `compare_runs(run_a, run_b)` — per-cell deltas, regressions highlighted.
  - `list_runs(limit?)`, `set_baseline(run_id)`.
  - `render_prompt(config_path, variant, case_id)` — fully-rendered messages,
    no model calls (same core function as `--dry-run`).
- The MCP layer contains no logic beyond tool schemas and calls into the core
  library.

### 4.7 CI gating and thresholds

Both modes, configurable per eval:

- **Absolute:** fail if aggregate score / pass rate drops below a configured
  threshold.
- **Regression vs baseline:** fail if results are worse than a designated
  baseline run (pinned by run id or a `baseline` alias users can point at a
  blessed run).

`evaling run` exits non-zero on threshold failure and says why.

### 4.8 Runs, storage, and outputs

Every run is persisted locally (default `.evaling/runs/<run-id>/`) as plain files:

```
run.json               # metadata: id, timestamps, config hash, aggregate scores, totals (cost, tokens)
config.snapshot.yaml   # exact config used, for reproducibility
results.jsonl          # one record per variant×model×case: messages sent, output,
                       # per-scorer results, usage, cost, latency, error
artifacts/             # content-addressed binary inputs/outputs
```

- `results.jsonl` is **append-as-completed**: an interrupted run keeps all
  finished records and can be resumed.
- Stored files are the source of truth; all exports are views over them:
  - `json` / `csv` — machine-readable, for CI and downstream analysis.
  - `md` — paste-into-a-PR summary.
  - `html` — a **single self-contained file** (inline CSS/JS, embedded data and
    images; opens from disk, no server): summary matrix, per-case drill-down with
    side-by-side outputs, failures-first sorting, judge rationales visible.
    Also available for `compare`.
- **Response caching, on by default:** identical (model, params, rendered
  messages, file content hashes) requests are served from a local cache, making
  scorer iteration free. `--no-cache` bypasses; cache location under `.evaling/`.

## 5. Non-functional requirements

- **Language/tooling:** Python ≥3.10; `uv` for project management; `ruff` for
  lint/format; `pytest` for tests. Installable via `uv tool install evaling` /
  `pipx install evaling`; published to PyPI once public.
- **Testing:** unit tests for config parsing, templating, content parts,
  providers, scorers, scorecard aggregation, storage, exports, CLI; integration
  tests run the full pipeline (including MCP server mode) against the mock
  provider — the suite never makes network calls. CI on GitHub Actions across
  supported Python versions. Coverage tracked; target ≥90% on core modules.
- **Reliability:** a single failing request must not abort a run — record the
  error, continue, and report it in the summary.
- **Distribution:** semantic versioning; changelog; minimal dependency footprint.
- **License:** MIT (adopted before the repo goes public).

## 6. Decision log

| Decision | Choice |
|---|---|
| Config format / templating | YAML + Jinja2 (strict undefined) |
| Conversations | Multi-turn in v1 |
| Binary inputs | Images + PDFs + audio in v1, via typed content parts |
| Providers (v1) | Anthropic, OpenAI, OpenAI-compatible, `command`, mock |
| Model calling via MCP | No (not an inference protocol); provider interface stays pluggable |
| Driving evaling via MCP | Yes — `--mcp` server in v1, aimed at agent iteration, not CI |
| Cache default | On (opt-out via `--no-cache`) |
| CI gating | Both absolute thresholds and regression-vs-baseline |
| HTML report | Yes — single self-contained file, for `export` and `compare` |
| Autorater evaluation | Meta-evals via `human_label` + agreement scorers |
| Settings resolution | flags > env (`EVALING_*`) > config `settings:` > user config > defaults |
| Output directory | Configurable at every layer; default `.evaling/runs/` |
| MCP entry point | `evaling mcp` subcommand (stdio) |
| MCP `run_eval` | Blocking, with progress notifications; async deferred |
| License | MIT |

## Appendix: example `eval.yaml`

```yaml
settings:            # project defaults; all overridable by env/flags
  output_dir: .evaling/runs
  concurrency: 8
  cache: true

models:
  - id: claude-sonnet-5
    provider: anthropic
    params: {max_tokens: 1024}
  - id: local-llama
    provider: openai-compatible
    base_url: http://localhost:11434/v1

variants:
  - name: concise
    prompt: prompts/concise.yaml
  - name: detailed
    prompt: prompts/detailed.yaml

cases:
  file: cases.jsonl

scorecard:
  - criterion: accuracy
    weight: 3
    scorer: {type: llm-judge, judge: quality-judge}
  - criterion: format
    weight: 1
    scorer: {type: json-schema, schema: schemas/answer.json}

judges:
  quality-judge:
    model: claude-sonnet-5
    rubric: prompts/judge-rubric.yaml

thresholds:
  min_pass_rate: 0.9
  baseline: regression   # fail if worse than pinned baseline
```
