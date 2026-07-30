# Getting started

## Install

Needs Python 3.10+ and [uv](https://docs.astral.sh/uv/). Not yet on PyPI, so
install from a checkout:

```sh
git clone https://github.com/amro/evaling && cd evaling
uv tool install .
evaling --version
```

That puts `evaling` on your `PATH`. To work on evaling itself, use the project
environment instead — `uv run evaling` always reflects your working tree,
whereas a tool install copies the code and needs `uv tool install --force .`
to pick up changes:

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
evaling doctor                 # is the key evaling needs actually resolving?
evaling run --dry-run          # validates config, renders every prompt, counts requests
evaling run --sample 10        # a random ten cases, to see whether it works at all
evaling run --max-cost 1.00    # hard ceiling on spend
```

`doctor` shows every setting with the layer that set it, which is the fastest
answer to "why isn't it using what I told it to use".

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
- [large-datasets.md](large-datasets.md) — streaming cases from your own API.
- [no-look.md](no-look.md) — evaluating data nobody may read.
