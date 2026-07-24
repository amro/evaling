# evaling — Requirements

**Status:** draft for discussion. Sections marked *(open question)* need decisions
before implementation.

## 1. Overview

`evaling` is an open-source command-line tool for evaluating LLM prompts. Its core
job: run one or more **prompt variants** against one or more **models** over a set
of **test cases**, score the outputs, and present a clear comparison so the user
can decide which prompt/model combination to ship.

### Goals

- Make A/B-comparing prompt variants and models a one-command operation.
- Terminal-first UX: readable tables/matrices, sensible defaults, no required web UI.
- Reproducible runs: results are stored and can be re-rendered, diffed, and shared.
- Extensible scoring: built-in checks plus user-defined Python scorers.
- Robust automated test suite from day one; no provider network calls in tests.

### Non-goals (v1)

- Not a hosted service, dashboard, or team collaboration platform.
- Not a prompt-management/versioning system (users keep prompts in their own repo).
- Not a tracing/observability tool for production traffic.
- No fine-tuning or training features.

## 2. Core concepts

| Concept | Description |
|---|---|
| **Eval config** | A file describing an eval: prompts, models, test cases, scorers. |
| **Prompt variant** | A named prompt template. Templates support variable substitution from test cases. |
| **Model** | A provider + model id + parameters (temperature, max tokens, etc.). |
| **Test case** | Input variables for the template, plus optional expected output / reference data for scoring. |
| **Scorer** | A function that grades a model output for a test case, producing a score and pass/fail. |
| **Run** | One execution of the full matrix: variants × models × test cases. Stored on disk with all outputs, scores, timing, token usage, and cost. |

## 3. Functional requirements

### 3.1 Configuration

- Eval configs are YAML files (`eval.yaml` by default). *(open question: confirm YAML vs TOML; YAML is the ecosystem norm — promptfoo, GitHub Actions.)*
- Prompts can be defined inline in the config or referenced as external files
  (e.g. `prompts/summarize-v2.txt`).
- Templates use a well-known syntax for variables. *(open question: Jinja2 vs
  simple `{var}` substitution. Jinja2 is more powerful; simple substitution is
  easier to reason about. Leaning Jinja2.)*
- Test cases can be listed inline or loaded from CSV/JSONL files.
- API keys come from environment variables (e.g. `ANTHROPIC_API_KEY`), never from
  config files.

### 3.2 Providers and models

- v1 providers: **Anthropic** and **OpenAI** as first-class, built-in providers.
- Provider interface is a small abstraction so new providers are easy to add
  (community contributions). *(open question: also ship an OpenAI-compatible
  generic provider — would cover Ollama/vLLM/OpenRouter/local models cheaply.)*
- Per-model parameters: temperature, max tokens, system prompt override, etc.
- Rate limiting and retry with backoff, configurable per provider.
- Concurrency: requests run in parallel with a configurable limit.

### 3.3 Scoring

Built-in scorers (v1):

- `exact` — output equals expected value.
- `contains` / `not-contains` — substring checks (case-sensitivity configurable).
- `regex` — output matches a pattern.
- `json-valid` / `json-schema` — output parses as JSON / matches a schema.
- `llm-judge` — another model grades the output against a rubric. Judge model and
  rubric are configurable.
- `python` — user-supplied Python function for custom scoring.

Multiple scorers can apply to one eval; each test case gets per-scorer results and
an overall pass/fail.

### 3.4 CLI

Command sketch (names open to bikeshedding):

- `evaling init` — scaffold an example `eval.yaml` and directory layout.
- `evaling run [config]` — run the eval matrix; stream progress; print summary.
- `evaling compare <run-a> <run-b>` — diff two runs (e.g. before/after a prompt change).
- `evaling show <run>` — re-render a stored run (summary, per-case detail, failures only).
- `evaling list` — list stored runs.
- `evaling export <run> --format json|csv|md` — machine-readable output for CI or docs.

UX requirements:

- Summary view: matrix of variants × models with aggregate scores, cost, latency.
- Detail view: drill into a single case's output(s) side by side.
- `--filter` flags to re-run or view subsets (one model, failing cases only, etc.).
- Exit code reflects pass/fail thresholds so `evaling run` works as a CI gate.
- Respect `NO_COLOR`; degrade gracefully in non-TTY environments.

### 3.5 Runs and storage

- Every run is persisted locally (default `.evaling/runs/<timestamp-id>/`) with:
  config snapshot, raw outputs, scores, token usage, cost estimate, timing.
- Response caching: identical (model, params, prompt) requests can be served from
  cache to make iterating on scorers free. Cache is opt-out. *(open question:
  opt-in vs opt-out default.)*
- Runs are plain files (JSON/JSONL) so users can inspect and version them.

## 4. Non-functional requirements

- **Language/tooling:** Python ≥3.10; `uv` for project management; `ruff` for
  lint/format; `pytest` for tests. Installable via `uv tool install evaling` /
  `pipx install evaling`; published to PyPI once public.
- **Testing:** unit tests for config parsing, templating, scorers, storage, CLI;
  integration tests run the full pipeline against a built-in **mock provider**
  (deterministic fake model) — the test suite never makes network calls. CI runs
  on GitHub Actions across supported Python versions. Coverage tracked; target
  ≥90% on core modules.
- **Reliability:** a single failing request must not abort a run — record the
  error, continue, and report it in the summary.
- **Cost safety:** show estimated request count before a run; `--max-cost` /
  confirmation guard for large matrices. *(open question: threshold behavior.)*
- **Distribution:** semantic versioning; changelog; minimal dependency footprint.

## 5. Open questions (to flesh out together)

1. Config format (YAML assumed) and template syntax (Jinja2 vs simple).
2. Should v1 include the generic OpenAI-compatible provider for local models?
3. Cache default: on (opt-out) or off (opt-in)?
4. Multi-turn conversations in v1, or single-turn only to start?
5. Baseline/threshold semantics for CI gating (absolute score vs regression vs baseline run).
6. License to adopt before going public (MIT vs Apache-2.0).
