# Changelog

All notable changes to evaling are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
