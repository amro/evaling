# Getting started

## Install

evaling is not yet on PyPI (coming with the first public release). From a
checkout:

```sh
uv sync
uv run evaling --version
```

## Your first eval, offline

```sh
evaling init
evaling run
```

`init` scaffolds a complete example — two prompt variants, three test cases, a
scorecard — wired to the built-in **mock provider**, so the run works with no
API keys and no network. You'll see a live progress bar, then the summary:

```
┃ Variant  ┃ Model ┃ Score ┃ Pass rate ┃ Cases ┃ Errors ┃
│ concise  │ mock  │ 1.000 │    100.0% │     3 │      0 │
│ detailed │ mock  │ 1.000 │    100.0% │     3 │      0 │
```

## Point it at real models

Edit the scaffolded `eval.yaml`'s `models:` block:

```yaml
models:
  - id: claude-sonnet-5
    provider: anthropic        # reads ANTHROPIC_API_KEY
  - id: gpt-5.2
    provider: openai           # reads OPENAI_API_KEY
  - id: llama3.1:8b            # local, no key needed
    provider: openai-compatible
    base_url: http://localhost:11434/v1
```

Keys come from environment variables only. See
[providers.md](providers.md) for every provider, including running local
models and evaluating your own scripts. Before spending money, preview what a
run will do:

```sh
evaling run --dry-run          # validates config, renders every prompt, counts requests
evaling run --max-cost 1.00    # hard ceiling on spend
```

## Read the results

```sh
evaling list                     # all stored runs
evaling show latest              # summary matrix
evaling show latest --failures   # what went wrong, and why
evaling show latest --case sky   # one case, side by side across variants/models
evaling compare <run-a> latest   # did my prompt change help?
evaling export latest --format md   # paste-ready summary
```

Every run is stored on disk (`.evaling/runs/`) and re-renderable forever; the
response cache makes repeat runs of unchanged cells free. See
[storage.md](storage.md).

## Where next

- [configuration.md](configuration.md) — everything `eval.yaml` can express.
- [prompts.md](prompts.md) — templating, multi-turn, images/PDFs/audio.
- [scoring.md](scoring.md) — scorecards, LLM judges, thresholds.
- [ci.md](ci.md) — gate your prompts in CI.
- [no-look.md](no-look.md) — cases from your own API, and evaluating data
  nobody may read.
