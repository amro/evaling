# Tutorial

A start-to-finish walkthrough: install evaling, run an eval with no API key,
point it at real models, grow it into something worth trusting, and wire it
into CI.

Every command here is runnable, and the output shown is real. The first half
needs no API key and no network — evaling ships a mock provider precisely so
you can learn the tool without spending anything. Real providers arrive in
[step 4](#4-point-it-at-a-real-model), by which point you'll know what a run
costs before you pay for one.

**Contents**

1. [Install](#1-install)
2. [Your first eval, offline](#2-your-first-eval-offline)
3. [Reading the results](#3-reading-the-results)
4. [Point it at a real model](#4-point-it-at-a-real-model)
5. [Test cases from a dataset](#5-test-cases-from-a-dataset)
6. [Scoring what exact match can't](#6-scoring-what-exact-match-cant)
7. [Comparing runs](#7-comparing-runs)
8. [Gating in CI](#8-gating-in-ci)
9. [Multimodal and multi-turn](#9-multimodal-and-multi-turn)
10. [Where to go next](#10-where-to-go-next)

---

## 1. Install

evaling needs Python 3.10 or newer.

```sh
uv tool install evaling      # or: pipx install evaling
evaling --version
```

Working from a checkout instead:

```sh
git clone https://github.com/amro/evaling && cd evaling
uv sync
uv run evaling --version
```

The rest of this tutorial writes `evaling`; from a checkout, prefix each
command with `uv run`.

## 2. Your first eval, offline

Make a directory and scaffold an example:

```sh
mkdir prompt-lab && cd prompt-lab
evaling init
```

```
created eval.yaml
created .gitignore
created .evaling.secrets.yaml.example
created prompts/concise.yaml
created prompts/detailed.yaml
created cases.jsonl

try it:  evaling run
```

`.gitignore` already excludes run output and the secrets file, so the
directory is safe to commit as-is.

Open `eval.yaml` — the whole format is four blocks:

```yaml
models:
  - id: mock                       # the offline stand-in; no key, no network
    provider: mock

variants:
  - name: concise
    prompt: prompts/concise.yaml
  - name: detailed
    prompt: prompts/detailed.yaml

cases:
  file: cases.jsonl

scorecard:
  - criterion: accuracy
    scorer: {type: contains}       # output must contain the case's `expected`

thresholds:
  min_pass_rate: 0.5               # `evaling run` exits non-zero below this
```

A prompt is a list of messages, in its own file or inline:

```yaml
# prompts/concise.yaml
- role: system
  content: Answer in one short sentence.
- role: user
  content: "{{ question }}"
```

And a case supplies the variables that template refers to. In a `.jsonl`
dataset, each line is one case with its variables at the top level:

```jsonl
{"id": "sky", "question": "What color is the sky on a clear day?", "expected": "sky"}
{"id": "capital", "question": "What is the capital of France?", "expected": "France"}
```

Together these define a **matrix**: every variant × every model × every case
is one *cell*. Two variants, one model, three cases is six cells, and evaling
runs them all.

Before running anything, check the config:

```sh
evaling validate
```

```
6 requests would be made; no model was called.
all prompts render cleanly
```

`validate` parses the config and renders every prompt with every case's
variables without calling a model. It catches the mistakes that would
otherwise cost money to discover: a typo'd variable, a missing file, a
template referring to something no case provides.

Now run it:

```sh
evaling run
```

```
┏━━━━━━━━━━┳━━━━━━━┳━━━━━━━┳━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━┓
┃ Variant  ┃ Model ┃ Score ┃ Pass rate ┃ Cases ┃ Errors ┃
┡━━━━━━━━━━╇━━━━━━━╇━━━━━━━╇━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━┩
│ concise  │ mock  │ 1.000 │    100.0% │     3 │      0 │
│ detailed │ mock  │ 1.000 │    100.0% │     3 │      0 │
├──────────┼───────┼───────┼───────────┼───────┼────────┤
│ overall  │       │ 1.000 │    100.0% │     6 │      0 │
└──────────┴───────┴───────┴───────────┴───────┴────────┘
6/6 succeeded, 0 failed, 0 cached — 103 in / 58 out tokens, $0.0000
gate passed
  ✓ min_pass_rate: pass rate 100.00% vs required 50.00%
run 20260724T215201475-b6f0 stored in .evaling/runs/20260724T215201475-b6f0
```

## 3. Reading the results

Every run is written to disk under `.evaling/runs/` and can be re-rendered
forever — you never have to re-run to look at something again.

```sh
evaling list                      # every stored run, newest first
evaling show latest               # the summary matrix again
evaling show latest --failures    # only the cells that failed, with reasons
evaling show latest --case sky    # one case, side by side across variants
```

`--failures` is the one you'll use most. A passing aggregate hides the
interesting part; this shows the prompt that went out, what came back, and
which criterion rejected it.

To get results somewhere else:

```sh
evaling export latest --format md       # paste into a PR or doc
evaling export latest --format csv      # spreadsheet
evaling export latest --format json     # scripting
evaling export latest --format html --out report.html
```

The HTML report is fully self-contained — one file, no JavaScript, no external
requests. Mail it, attach it to a CI build, open it from a thumb drive.

## 4. Point it at a real model

### Give evaling your API key

**Never put an API key in `eval.yaml`.** That file belongs in git; keys don't.
evaling reads keys from the environment, or from a secrets file that
`evaling init` has already gitignored for you:

```sh
cp .evaling.secrets.yaml.example .evaling.secrets.yaml
chmod 600 .evaling.secrets.yaml
```

```yaml
# .evaling.secrets.yaml — gitignored, never commit
ANTHROPIC_API_KEY: sk-ant-...
OPENAI_API_KEY: sk-...
```

Environment variables always win over the file, so CI injects secrets the
usual way with no config change. evaling warns if the file is readable by
anyone but you, and redacts key values from error messages. Details in
[secrets.md](secrets.md).

### Add the models

```yaml
models:
  - id: claude-sonnet-5
    provider: anthropic
    params: {max_tokens: 1024}
  - id: gpt-5.2
    provider: openai
```

The `id` is the provider's model name and doubles as the label in reports.
`evaling init --provider anthropic` scaffolds this for you.

### Find out what it costs before you pay for it

```sh
evaling validate                 # how many requests would this make?
evaling run --max-cost 1.00      # hard ceiling: stop issuing calls at $1
```

`--max-cost` is admission control, not a post-hoc check: evaling stops
*issuing* calls once accumulated spend reaches the ceiling, and reports how
many cells it skipped. Start with a small number.

While iterating, cut the matrix down instead of running all of it:

```sh
evaling run --variant concise --case sky     # one cell
evaling run --model gpt-5.2                  # one model
```

Responses are cached, so re-running an unchanged cell is free and instant.
The cache key covers only what changes the answer — model, prompt, request
parameters — so relabeling a run or adding a case doesn't invalidate what you
already paid for.

```sh
evaling cache info                  # where it lives, how many entries
evaling run --no-cache              # bypass it for one run
evaling cache clear --older-than 30 # prune entries older than 30 days
```

### Local and other-vendor models

Anything speaking the OpenAI chat-completions API works through
`openai-compatible` — Ollama, LM Studio, vLLM, llama.cpp, OpenRouter, and
Gemini/Gemma via Google's OpenAI-compatible endpoint:

```yaml
models:
  - id: llama3.1:8b
    provider: openai-compatible
    base_url: http://localhost:11434/v1     # Ollama; no key needed

  - id: gemini-2.5-pro
    provider: openai-compatible
    base_url: https://generativelanguage.googleapis.com/v1beta/openai/
    api_key_env: GEMINI_API_KEY
```

To evaluate something that isn't an HTTP API at all — your own agent, a RAG
pipeline, a shell script — use the `command` provider, which pipes JSON to a
program of your choosing. See [providers.md](providers.md).

### Keeping inside rate limits

If a provider throttles you, cap that model rather than slowing the whole
matrix:

```yaml
models:
  - id: claude-sonnet-5
    provider: anthropic
    max_concurrency: 4          # at most 4 in flight for this model
    requests_per_minute: 50     # and no more than 50/min
```

Both are per model and compose with the global `--concurrency`, so a fast
local model isn't held back by a rate-limited hosted one.

## 5. Test cases from a dataset

Inline `cases:` is fine for a handful:

```yaml
cases:
  - id: greeting
    vars: {question: "hello?"}
    expected: "hello?"
```

Past a dozen, move them to a file. Note the shape difference: inline cases
nest variables under `vars`, while dataset rows put them at the top level and
evaling collects the leftovers into `vars` for you.

```yaml
cases:
  file: cases.jsonl
```

```jsonl
{"id": "capital-fr", "question": "Capital of France?", "expected": "Paris"}
{"id": "capital-jp", "question": "Capital of Japan?", "expected": "Tokyo"}
```

CSV works the same way — a header row of variable names, one case per row:

```csv
id,question,expected
capital-fr,Capital of France?,Paris
capital-jp,Capital of Japan?,Tokyo
```

Keep the dataset in git next to the config. The two change together, and a run
is only reproducible if both are pinned.

## 6. Scoring what exact match can't

`contains` and `exact` handle a surprising amount, and you should reach for
them first — free, instant, deterministic. The full set:

| Scorer | Passes when |
| --- | --- |
| `exact` | Output equals `expected` |
| `contains` / `not-contains` | Substring present / absent |
| `regex` | Pattern matches |
| `json-valid` | Output parses as JSON |
| `json-schema` | Output validates against a JSON Schema |
| `python` | Your own function returns a passing score |
| `llm-judge` | A model grades the output against your rubric |
| `agreement` | The verdict matches your `human_label` |

A scorecard combines several, with weights:

```yaml
scorecard:
  - criterion: mentions-capital
    scorer: {type: contains}
    weight: 2
  - criterion: no-hedging
    scorer: {type: not-contains, value: "I think"}
    weight: 1
```

Anything the scorer needs goes beside `type`; the options for each are listed
in [scoring.md](scoring.md).

### Your own logic

When the rule is real but not expressible as a pattern, write it:

```yaml
scorecard:
  - criterion: has-all-facts
    scorer: {type: python, file: scorers/grade.py}
```

```python
# scorers/grade.py — sync or async
def score(output: str, case: dict):
    return {"score": 0.9, "passed": True, "detail": "matched 9/10 facts"}
```

### When you need judgment

For "is this a good explanation?" there's no substring to match. Define a
judge — a judge model plus a rubric prompt — and score against it:

```yaml
judges:
  quality:
    model: claude-sonnet-5           # an id from your models block
    rubric: prompts/judge-rubric.yaml

scorecard:
  - criterion: helpful
    scorer: {type: llm-judge, judge: quality}
```

```yaml
# prompts/judge-rubric.yaml
- role: system
  content: >
    Grade the answer 1-5 for accuracy. Respond with JSON:
    {"score": <1-5>, "rationale": "<one sentence>"}
- role: user
  content: |
    Question: {{ vars.question }}
    Expected: {{ expected }}
    Answer: {{ output }}
```

The rubric receives `output`, `expected`, and `vars`. The judge answers with
JSON containing a numeric `score`; the `rationale` is stored and shown in
reports, which matters more than it sounds — when a judge disagrees with you,
the rationale is how you find out why.

A judge is a model call, so it costs money and it can be wrong. Before
trusting one to gate anything, check it against labels you wrote yourself:
put your grade in each case's `human_label` and score the judge with the
`agreement` scorer. [evaluating-judges.md](evaluating-judges.md) walks through
it, and it will save you from shipping a rubric that quietly disagrees with
you.

## 7. Comparing runs

The reason the tool exists. Change your prompt, re-run, and diff:

```sh
evaling run --label before
# ... edit the prompt ...
evaling run --label after
evaling compare before after
```

You get per-cell score deltas and the pass-rate change, so you can see not
just *whether* it improved but *which cases moved* — including the ones your
change quietly broke while the average went up.

```sh
evaling compare before after --html diff.html
```

## 8. Gating in CI

Turn thresholds into a build failure:

```yaml
thresholds:
  min_pass_rate: 0.9
  min_score: 0.75
```

`evaling run` exits non-zero when a threshold isn't met, so CI fails on its
own. To catch regressions rather than absolute quality, pin a baseline:

```sh
evaling baseline set latest
```

```yaml
thresholds:
  baseline: regression      # also fail if this run is worse than the baseline
```

Absolute and baseline checks can apply together; each check's outcome is
recorded in the run's `run.json` under `gate`.

A minimal GitHub Actions job:

```yaml
- run: uv tool install evaling
- run: evaling run --max-cost 5.00 --html report.html
  env:
    ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
- uses: actions/upload-artifact@v4
  if: always()
  with: {name: eval-report, path: report.html}
```

Keep `--max-cost` on CI runs. A misconfigured matrix in a loop is the
expensive kind of mistake, and the ceiling makes it a cheap one. More
patterns — caching between runs, PR comments, scheduled drift checks — in
[ci.md](ci.md).

## 9. Multimodal and multi-turn

A prompt can be a whole conversation, not just one message:

```yaml
# prompts/chat.yaml
- role: system
  content: "You are a {{ persona }}."
- role: user
  content: "{{ opening }}"
- role: assistant
  content: "Understood. Tell me more."
- role: user
  content: "{{ followup }}"
```

And cases can carry images, PDFs, audio, and video. Attachments go in the
case's `files` map and are referenced as `{{ files.<name> }}`:

```yaml
variants:
  - name: describe
    prompt:
      - role: user
        content:
          - text: "{{ instruction }}"
          - image: "{{ files.photo }}"
          - file: "{{ files.doc }}"

cases:
  - id: chart
    vars: {instruction: "What does this chart show?"}
    files: {photo: data/chart.png, doc: data/report.pdf}
    expected: revenue
```

In a dataset, mark an attachment with the `file://` prefix and evaling
resolves it relative to the dataset file:

```jsonl
{"id": "chart", "instruction": "What does this chart show?", "photo": "file://data/chart.png"}
```

evaling checks that the model supports the media type before sending, so an
unsupported combination fails immediately with a clear message instead of a
confusing provider error.

Four complete, runnable examples live in [`examples/`](../examples/) — text
and multimodal, single- and multi-turn. The test suite runs all four on every
commit, so they're guaranteed to work with the current version. Copy one as a
starting point:

```sh
cp -r examples/media-single my-eval && cd my-eval && evaling run
```

## 10. Where to go next

- [configuration.md](configuration.md) — every `eval.yaml` field, settings
  layering, environment variables
- [prompts.md](prompts.md) — templating, multi-turn, media, dataset formats
- [scoring.md](scoring.md) — every scorer in detail, custom Python scorers
- [providers.md](providers.md) — all providers, pricing, rate limits, writing
  your own
- [storage.md](storage.md) — the run format, resuming, programmatic access
- [cli.md](cli.md) — full command reference
- [troubleshooting.md](troubleshooting.md) — when something doesn't work
