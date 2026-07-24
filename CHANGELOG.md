# Changelog

All notable changes to evaling are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
