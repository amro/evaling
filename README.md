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

Full docs live in [`docs/`](docs/). New here? Start with the
**[tutorial](docs/tutorial.md)** — install through CI gating, with runnable
examples at every step.

- [Tutorial](docs/tutorial.md) — the complete walkthrough.
- [Getting started](docs/getting-started.md) — the short version.
- [CLI reference](docs/cli.md) — commands, flags, run references, exit codes.
- [Configuration](docs/configuration.md) — the `eval.yaml` reference, settings
  layering, and environment variables.
- [Prompts](docs/prompts.md) — variants, Jinja2 templating, multi-turn
  messages, multimodal inputs, and case datasets.
- [Scoring](docs/scoring.md) — scorers, scorecards, LLM judges, and CI
  thresholds.
- [Providers](docs/providers.md) — Anthropic, OpenAI, OpenAI-compatible (local
  models, Gemini, OpenRouter), the `command` provider, pricing, and adding your
  own.
- [Secrets](docs/secrets.md) — where API keys come from, and how they're kept
  out of git and out of your output.
- [Storage](docs/storage.md) — the run directory format, resuming interrupted
  runs, response caching, and programmatic access.
- [CI recipes](docs/ci.md) — gating, baselines, cost ceilings, report
  artifacts.
- [Evaluating judges](docs/evaluating-judges.md) — calibrate autoraters against
  human labels (meta-evals).
- [MCP server](docs/mcp.md) — drive evaling from an agent for hands-off prompt
  iteration.
- [Python API](docs/python-api.md) — use evaling as a library.
- [Troubleshooting](docs/troubleshooting.md) — symptoms, causes, fixes.
- [Architecture](docs/architecture.md) — how it's built, and why.

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
[`examples/`](examples/) — the test suite runs them end to end against the
built-in mock provider on every commit, so they are guaranteed to work with
the current version.

See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.

## License

[MIT](LICENSE)
