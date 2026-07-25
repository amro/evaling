# Support triage

A realistic eval: classify support tickets by category and urgency, and return
JSON a downstream system can actually use.

Runs offline. `fake_model.py` is a deterministic stand-in for a real model,
invoked through the `command` provider — the same mechanism you would use to
evaluate an agent, a RAG pipeline, or a local binary.

```sh
cd examples/support-triage
evaling validate      # check the config; call nothing
evaling run           # 2 variants x 3 models x 12 cases = 72 cells
```

```
┃ Variant    ┃ Model      ┃ Score ┃ Pass rate ┃ Cases ┃ Errors ┃
│ structured │ clean-json │ 0.979 │     91.7% │    12 │      0 │
│ structured │ sloppy     │ 0.792 │     75.0% │    12 │      0 │
│ structured │ chatty     │ 0.333 │      0.0% │    12 │      0 │
│ terse      │ clean-json │ 0.938 │     83.3% │    12 │      0 │
│ terse      │ sloppy     │ 0.771 │     66.7% │    12 │      0 │
│ terse      │ chatty     │ 0.333 │      0.0% │    12 │      0 │
```

## What it demonstrates

**Prompts matter, measurably.** `structured` spells out what "urgent" means;
`terse` doesn't. The fake model behaves accordingly, and the eval shows the gap
— which is the whole point of the tool.

**Models fail in different ways.** `chatty` wraps its JSON in prose and code
fences; `sloppy` emits malformed JSON on the harder cases. Both are real
failure modes, and each is caught by a different criterion.

**A weighted scorecard.** Being right (`correct-triage`, weight 3) counts for
more than being well-formed, but format still counts:

| Criterion | Scorer | Catches |
| --- | --- | --- |
| `correct-triage` | `python` | Wrong category or urgency |
| `valid-json` | `json-valid` | Output that won't parse |
| `matches-schema` | `json-schema` | Parses, but the wrong shape |
| `no-prose` | `not-contains` | Chatter around the answer |

## Things to try

```sh
evaling show latest --failures          # what failed, and why
evaling show latest --case t-1002       # one ticket across all six cells
evaling export latest --format html --out report.html

evaling run --variant structured --label structured-only
evaling compare structured-only latest  # did narrowing help?
```

Then edit `prompts/terse.yaml` to define urgency, re-run, and compare — you
should see `terse` close the gap.

## Using a real model

Replace the models block:

```yaml
models:
  - id: claude-sonnet-5
    provider: anthropic
    params: {max_tokens: 256}
```

Supply a key via the environment or a gitignored secrets file
([secrets.md](../../docs/secrets.md)), then:

```sh
evaling validate                 # how many requests would this make?
evaling run --max-cost 0.50      # ceiling first
```

The scorecard needs no changes — that's the point of keeping scoring separate
from the model.
