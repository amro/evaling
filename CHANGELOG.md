# Changelog

All notable changes to evaling are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
