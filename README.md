# evaling

[![CI](https://github.com/amro/evaling/actions/workflows/ci.yml/badge.svg)](https://github.com/amro/evaling/actions/workflows/ci.yml)

A command-line tool for comparing prompt variants and models, easily.

> **Status:** early development — not yet ready for use. See [REQUIREMENTS.md](REQUIREMENTS.md) for the current design.

## Why

When you're iterating on a prompt, you want fast answers to questions like:

- Which of these three phrasings performs best on my test cases?
- Does the cheaper model handle this prompt as well as the expensive one?
- Did my latest prompt tweak regress anything?

`evaling` runs your prompt variants against your chosen models over a set of test
cases, scores the outputs, and shows you a comparison — all from the terminal.

## Quick taste

```sh
evaling init      # scaffold a working example (offline, mock provider)
evaling run       # run it: progress bar, summary matrix, pass/fail gate
evaling show latest --failures
evaling compare <run-a> <run-b>
evaling export latest --format md
```

See [getting started](docs/getting-started.md).

## Documentation

Detailed docs live in [`docs/`](docs/):

- [Getting started](docs/getting-started.md) — install, first eval, reading
  results.
- [CLI reference](docs/cli.md) — commands, flags, run references, exit codes.
- [CI recipes](docs/ci.md) — gating, baselines, cost ceilings, report
  artifacts.
- [Configuration](docs/configuration.md) — the `eval.yaml` reference, settings
  layering, and environment variables.
- [Prompts](docs/prompts.md) — variants, Jinja2 templating, multi-turn
  messages, multimodal inputs, and case datasets.
- [Scoring](docs/scoring.md) — scorers, scorecards, LLM judges, and CI
  thresholds.
- [Evaluating judges](docs/evaluating-judges.md) — calibrate autoraters against
  human labels (meta-evals).
- [Storage](docs/storage.md) — the run directory format, resuming interrupted
  runs, response caching, and programmatic access.

## Development

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync              # create the venv and install dependencies
uv run evaling       # run the CLI
uv run pytest        # run the test suite
uv run ruff check .  # lint
uv run ruff format . # format
```

Changes are documented in the [CHANGELOG](CHANGELOG.md).

Complete sample evals (text and multimodal, single- and multi-turn) live in
[`tests/fixtures/e2e/`](tests/fixtures/e2e/) — the test suite runs them end to
end against the built-in mock provider, and they double as worked examples of
the config format.

## License

[MIT](LICENSE)
