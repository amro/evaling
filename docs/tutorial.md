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
10. [Scaling up: cases from your own systems](#10-scaling-up-cases-from-your-own-systems)
11. [Where to go next](#11-where-to-go-next)

---

## 1. Install

evaling needs Python 3.10 or newer and [uv](https://docs.astral.sh/uv/):

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then install evaling:

```sh
uv tool install evaling
evaling --version
```

That puts `evaling` on your `PATH`, which is what the rest of this tutorial
assumes. If the command isn't found afterwards, `uv tool update-shell` fixes
your shell profile.

**If you plan to change evaling itself**, use the project environment instead —
`uv run` always reflects your working tree:

```sh
git clone https://github.com/amro/evaling && cd evaling
uv sync
uv run evaling --version
```

`uv tool install` copies the code rather than linking it, so a tool install
keeps running the version you installed until you re-run
`uv tool install --force .`.

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
  - criterion: cites-the-signal
    scorer: {type: contains}       # output must contain the case's `expected`

thresholds:
  min_pass_rate: 0.5               # `evaling run` exits non-zero below this
```

A prompt is a list of messages, in its own file or inline:

```yaml
# prompts/concise.yaml
- role: system
  content: >-
    You review card transactions for fraud. Answer in one sentence: the verdict,
    then the single detail that decided it, quoted from the transaction.
- role: user
  content: "{{ transaction }}"
```

And a case supplies the variables that template refers to. In a `.jsonl`
dataset, each line is one case with its variables at the top level:

```jsonl
{"id": "midnight-watch", "transaction": "$2,480 at Luxe Watches Ltd, 03:14 local time, card not present, first purchase from this merchant", "expected": "card not present"}
{"id": "weekly-grocery", "transaction": "$63.19 at Fairview Grocery, 18:22 local time, chip read, same store weekly for two years", "expected": "chip read"}
```

The scaffold grades whether the model *cited the right signal*, not whether it
guessed the right label. Each `expected` is quoted from its own transaction
and appears in no other, so an output that names the wrong detail fails — swap
two of them and the run drops to 33%.

That is also what makes the scaffold work offline. The mock provider echoes
the prompt instead of answering it, and the deciding detail is in the prompt,
so the same cases pass before and after you wire up a real model.

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
6/6 succeeded, 0 failed, 0 cached — 379 in / 241 out tokens, $0.0000
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
evaling show latest --case retry-burst   # one case, across variants
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

Check that evaling can see a key before spending anything:

```sh
evaling doctor
```

It reports each model's API-key variable and whether it resolves, along with
every setting and the layer that supplied it. No network unless you add
`--check-providers`.

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

A key also needs a funded account — API credits are separate from a Claude or
ChatGPT subscription, and a brand-new key with no credit fails on the first
request rather than at setup.

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
evaling validate                 # how many requests, and roughly what it costs
evaling run --max-cost 1.00      # hard ceiling: stop issuing calls at $1
```

`validate`, `run --dry-run`, and `run` itself all print an estimate before
any call goes out, where the models are ones evaling has rates for:

```
12 requests would be made; no model was called.
  estimated ~$0.38
```

Deliberately hedged. Token counts are approximated from the rendered prompts,
retries bill again, and the built-in price table is a convenience rather than
an invoice — so treat it as an order of magnitude. `--max-cost` is what
actually holds.

`--max-cost` is admission control, not a post-hoc check: evaling stops
*issuing* calls once accumulated spend reaches the ceiling, and reports how
many cells it skipped. Start with a small number.

While iterating, cut the matrix down instead of running all of it:

```sh
evaling run --variant concise --case retry-burst   # one cell
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
    base_url: https://generativelanguage.googleapis.com/v1beta/openai
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

`contains` and `exact` cover a lot, and they're free, instant, and
deterministic. The full set:

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
models:
  - id: gpt-5.2
    provider: openai
  - id: claude-sonnet-5
    provider: anthropic
    role: judge                      # grades; not evaluated itself

judges:
  quality:
    model: claude-sonnet-5
    rubric: prompts/judge-rubric.yaml

scorecard:
  - criterion: helpful
    scorer: {type: llm-judge, judge: quality}
```

`role: judge` is required on a judge's model. Without it the model would also
be evaluated as a candidate, which doubles the run and produces a row where
the judge grades its own output. Use `role: both` if you do want it graded as
well.

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
it — a rubric can disagree with you consistently without that being visible
in the scores.

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

While a prompt is still moving, `--sample N` draws a random N of your *cases*
and runs the whole matrix over those — every variant against every model, on
fewer cases. Both runs need the same draw to be comparable, so pass the seed
the first one reports:

```sh
evaling run --sample 20 --label before
# reported: repeat this draw with --sample 20 --sample-seed 2894127714
evaling run --sample 20 --sample-seed 2894127714 --label after
```

`compare` warns if you forget — two different draws differ partly by prompt
and partly by which cases they happened to draw.

```sh
evaling compare before after --html diff.html
```

### Is the difference real?

Before you act on a delta, check it against how many cases produced it. For a
pass rate near 50% — the widest case — at 95% confidence:

| Cases | Margin of error |
| --- | --- |
| 30 | ±18 points |
| 100 | ±10 points |
| 400 | ±5 points |
| 1,000 | ±3 points |

So 72% against 78% on 50 cases is not a result; it's noise. Comparing two
variants over the *same* cases is more sensitive than those numbers suggest —
what matters there is how many cases the two disagree on, not the total — but
the direction of the caution holds either way.

The cheapest fix for an inconclusive comparison is usually more cases rather
than more tweaking. See
[how many cases do you need](large-datasets.md#how-many-cases-do-you-need).

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
- uses: actions/upload-artifact@v7
  if: always()
  with: {name: eval-report, path: report.html}
```

A `--max-cost` ceiling on CI runs bounds what a misconfigured matrix can
spend before anyone notices. More patterns — caching between runs, PR
comments, scheduled drift checks — in [ci.md](ci.md).

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

And cases can carry images, PDFs, audio (`.mp3`, `.wav`, `.ogg`, `.flac`,
`.m4a`, `.aac`), and video. Attachments go in the case's `files` map and are
referenced as `{{ files.<name> }}`:

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

Six complete, runnable examples live in [`examples/`](../examples/), all
offline. [`support-triage/`](../examples/support-triage/) is the most
realistic one — ticket classification across two prompt variants and three
models, with a weighted scorecard and a Python scorer. The test suite runs
every example on each commit, so they can't drift from the code. Copy one as a
starting point:

```sh
cp -r examples/support-triage my-eval && cd my-eval && evaling run
```

## 10. Scaling up: cases from your own systems

Inline cases and dataset files both assume your cases fit in a file and are
yours to check in. Past a certain size that stops being true — the cases live
in a warehouse or a ticket system, and there are hundreds of thousands of them.

### A case source

Instead of a file, point at Python you write. evaling calls it for pages:

```python
# sources/prod_tickets.py
from evaling import BaseCaseSource, Case, CasePage


class ProdTickets(BaseCaseSource):
    def fetch(self, cursor, limit):
        page = my_api.query(after=cursor, limit=limit)
        return CasePage(
            cases=[Case(id=row["id"], vars={"ticket": row["body"]}) for row in page.rows],
            cursor=page.next_cursor,  # None when there is no more
        )

    def count(self):  # optional; gives progress a real total
        return my_api.total()


def make_source():
    return ProdTickets()
```

```yaml
cases:
  source: sources/prod_tickets.py:make_source
  page_size: 200
  limit: 5000
```

`fetch` may be `async def`. You don't inherit from anything if you'd rather
not — `CaseSource` is a Protocol, so any object with a `fetch` method works
and your class can know nothing about evaling.

Cases stream a page at a time, so a run over a hundred thousand cases costs no
more memory than a run over ten. Two things change: `--case` filtering can't
work (evaling doesn't know the ids in advance — filter inside your source),
and a source with no `limit` refuses to start unless you pass `--max-cost`. An
source with no bound will run for as many cases as it returns.

```sh
evaling validate     # fetches one page and renders it — no full walk
```

### When you also can't read the data

A narrower case, layered on the same mechanism: the rows are production
traffic and nobody is permitted to read them afterwards. This is a separate
feature — most large-dataset runs don't need it — but the two pair naturally,
since data you may not read tends to live in a system rather than a file.

```yaml
privacy:
  no_look: true
```

Prompts, model outputs, judge rationales, attachments, and provider error
bodies are dropped before anything is written or displayed. What survives is
what you actually wanted: scores, pass rates, token counts, latency, and the
gate.

```json
{"case_id": "case-2be299046ace7bbe", "variant": "acknowledge", "model": "mock",
 "messages": [], "output": null, "input_tokens": 52,
 "scores": {"brief": {"score": 1.0, "passed": true,
            "detail": "within the 60-word limit"}}}
```

Case ids are hashed by default, because an id from a production system is
often an email address or an order number — it identifies a record as surely
as the record does. The hash is stable, so you can still follow one case
across the matrix and between runs.

**The catch, and the way around it.** You can't debug a regression by reading
failures — that's the point, and it also removes your normal way of working.
The escape valve is the scorer: it sees the real output and decides what is
safe to say about it.

```python
def score(output: str, case: dict):
    missing = [f for f in REQUIRED if f not in output]
    if missing:
        return {"score": 0.0, "passed": False, "detail": f"missing fields: {', '.join(missing)}"}
    return {"score": 1.0, "passed": True, "detail": "complete"}
```

That `detail` survives no-look mode, because you wrote it. A judge's rationale
does not — it quotes the text it graded.

Verify the claim rather than trusting it:

```sh
cd examples/no-look
evaling run
grep -r "@example.com" .evaling/     # nothing
```

Two more things worth knowing before you rely on this. **Runs can't be
resumed** when cases come from a source: a live source can return different
rows on the second call, and a run whose halves describe different data looks
completely normal while being wrong. And **a judge still sends your data to
another vendor** — rationales are dropped from artifacts, but the data leaves
your process to reach the judge, which is a decision only you can make.

Full reference: [large-datasets.md](large-datasets.md) for sources,
[no-look.md](no-look.md) for the privacy mode.

## 11. Where to go next

- [configuration.md](configuration.md) — every `eval.yaml` field, settings
  layering, environment variables
- [prompts.md](prompts.md) — templating, multi-turn, media, dataset formats
- [scoring.md](scoring.md) — every scorer in detail, custom Python scorers
- [providers.md](providers.md) — all providers, pricing, rate limits, writing
  your own
- [large-datasets.md](large-datasets.md) — case sources and streaming at scale
- [no-look.md](no-look.md) — evaluating data nobody may read
- [storage.md](storage.md) — the run format, resuming, programmatic access
- [python-api.md](python-api.md) — using evaling as a library
- [cli.md](cli.md) — full command reference
- [troubleshooting.md](troubleshooting.md) — when something doesn't work
