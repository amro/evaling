# Examples

Four complete evals, each runnable as-is. They use the built-in mock provider,
so they need no API key and no network.

The test suite runs all four end to end on every commit
(`tests/test_e2e.py`), which means they can't drift from the code — if an
example here is wrong, CI is red.

| Example | Shows |
| --- | --- |
| [`text-single/`](text-single/) | Text, single-turn, inline prompts and cases. Two variants × two models × two cases. |
| [`text-multi/`](text-multi/) | A multi-turn conversation, an external prompt file, and a JSONL dataset. |
| [`media-single/`](media-single/) | Every media kind — image, PDF, audio, video — with cases from a CSV using the `file://` convention. |
| [`media-multi/`](media-multi/) | Media spread across turns: an image in the first user turn, a PDF plus text in the second. |

## Running one

```sh
cd examples/text-single
evaling run
```

Or copy one as the starting point for your own:

```sh
cp -r examples/media-single my-eval
cd my-eval
evaling validate      # check it before spending anything
evaling run
```

## Making them call a real model

Each example's `eval.yaml` starts with a mock model:

```yaml
models:
  - id: mock-echo
    provider: mock
```

Swap in a real one and supply a key via the environment or a gitignored
secrets file ([secrets.md](../docs/secrets.md)):

```yaml
models:
  - id: claude-sonnet-5
    provider: anthropic
    params: {max_tokens: 1024}
```

The mock provider echoes its input, so the scorecards here are written around
that behavior — expect to adjust `expected` values and criteria once a real
model is answering.

New to the config format? The [tutorial](../docs/tutorial.md) builds one of
these up from scratch.
