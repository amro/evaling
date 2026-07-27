# evaling

[![CI](https://github.com/amro/evaling/actions/workflows/ci.yml/badge.svg)](https://github.com/amro/evaling/actions/workflows/ci.yml)

A command-line tool for comparing prompt variants and models, easily.

> **Status:** early development — **alpha testers wanted.** It works end to
> end and the test suite is thorough, but it's early: expect gaps where the
> tests don't reach, and expect the config format to shift a little before 1.0.
> If you try it, [open an issue](https://github.com/amro/evaling/issues) —
> rough edges, confusing output, and missing providers are all worth
> reporting.

## Why

When you're iterating on a prompt, you want fast answers to questions like:

- Which of these three phrasings performs best on my test cases?
- Does the cheaper model handle this prompt as well as the expensive one?
- Did my latest prompt tweak regress anything?

`evaling` runs your prompt variants against your chosen models over a set of test
cases, scores the outputs, and shows you a comparison — all from the terminal.

## What you can evaluate

**Anything you can call.** Anthropic and OpenAI directly; Ollama, vLLM, LM
Studio, OpenRouter, Gemini and Gemma through the OpenAI-compatible endpoint;
and anything else at all through the `command` provider, which pipes JSON to a
script — so an agent, a RAG pipeline, or a local binary is as evaluable as a
chat API.

**Text, and not only text.** Single prompts or scripted multi-turn
conversations. Images, PDFs, audio, and video attach to cases and are checked
against each model's declared capabilities before a request is sent.

**Against whatever "correct" means for you.** Exact match, substring, regex,
and JSON-schema validation for the mechanical parts; your own Python function
when the rule is real but not a pattern; an LLM judge with a rubric when the
question is genuinely a matter of judgment — and a meta-eval to check that
judge against labels you wrote yourself, before you trust it to gate anything.

**At the size you actually have.** A dozen cases inline in the config, a JSONL
or CSV dataset in git, or hundreds of thousands streamed from your own
warehouse or API through a [case source](docs/large-datasets.md) — Python you
write that evaling pages through. Memory is bounded by concurrency, not by
case count, so a 500,000-case run costs no more up front than a ten-case one.

**Including data you're not allowed to read.** For the narrower case where
the data is off-limits to humans, [no-look mode](docs/no-look.md) — *eyes-off*
evaluation, in privacy terms — keeps prompts, outputs, and attachments out of
every artifact: you get the scores, the data stays where it was.

**And it tells you whether the change helped.** Compare two runs cell by cell,
pin a baseline, and fail CI on a regression rather than on a hunch.

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

## How this was built

evaling was written with heavy use of AI coding tools — most of it with Claude
Code, working from requirements and design decisions recorded in
[REQUIREMENTS.md](REQUIREMENTS.md). Every change is reviewed before it lands,
carries tests, and has to pass the same CI as anything else: lint, formatting,
a dependency audit, and the full suite on Linux, macOS, and Windows across
Python 3.10–3.13. The docs and the worked examples are exercised by that suite
too, so they cannot quietly drift from the code.

Worth saying plainly rather than leaving you to infer it from the commit log.

## License

[MIT](LICENSE)
