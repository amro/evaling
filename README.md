# evaling

A command-line tool for comparing prompt variants and models, easily.

> **Status:** early development — not yet ready for use. See [REQUIREMENTS.md](REQUIREMENTS.md) for the current design.

## Why

When you're iterating on a prompt, you want fast answers to questions like:

- Which of these three phrasings performs best on my test cases?
- Does the cheaper model handle this prompt as well as the expensive one?
- Did my latest prompt tweak regress anything?

`evaling` runs your prompt variants against your chosen models over a set of test
cases, scores the outputs, and shows you a comparison — all from the terminal.

## Documentation

Detailed docs live in [`docs/`](docs/):

- [Configuration](docs/configuration.md) — the `eval.yaml` reference, settings
  layering, and environment variables.

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

## License

[MIT](LICENSE)
