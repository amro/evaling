# Changelog

All notable changes to evaling are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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

### Fixed

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
