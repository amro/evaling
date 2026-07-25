# Configuration

evaling is configured by an **eval config** (usually `eval.yaml`) that defines
what to evaluate, plus **workspace settings** that control where and how runs
execute.

- The eval config is portable: check it into your repo and everyone runs the
  same eval.
- Workspace settings are machine/user concerns (output directory, concurrency,
  caching) and can be set at several layers — see
  [Settings resolution](#settings-resolution).

Validation is strict: unknown or misspelled keys anywhere in the config are
errors, except inside `scorer:` blocks where extra keys are scorer parameters.

## Top-level structure

```yaml
settings:   # optional — workspace defaults for this project
models:     # required — the models to evaluate
variants:   # required — the prompt variants to compare
cases:      # required — test cases (inline list or file reference)
scorecard:  # required — how outputs are scored
judges:     # optional — LLM judge definitions used by llm-judge scorers
thresholds: # optional — pass/fail gates for CI
```

Relative paths inside the config (prompt files, case files, attachments)
resolve relative to the config file's directory.

## `settings`

```yaml
settings:
  output_dir: .evaling/runs    # where runs are stored
  cache_dir: .evaling/cache    # response cache location
  concurrency: 8               # max parallel model requests (>= 1)
  cache: true                  # serve identical requests from cache
```

All fields are optional; only the fields you set here override lower layers.

## `models`

```yaml
models:
  - id: claude-sonnet-5          # unique name, referenced by judges/filters
    provider: anthropic
    params: {max_tokens: 1024}   # provider/model parameters, passed through
  - id: local-llama
    provider: openai-compatible
    base_url: http://localhost:11434/v1
  - id: my-agent
    provider: command
    command: ./run-agent.sh
```

Providers (full reference: [providers.md](providers.md)):

| Provider | Notes |
|---|---|
| `anthropic` | Uses `ANTHROPIC_API_KEY`. |
| `openai` | Uses `OPENAI_API_KEY`. |
| `openai-compatible` | Requires `base_url`. Covers Ollama, vLLM, OpenRouter, etc. |
| `command` | Requires `command`. Shells out: request on stdin, response on stdout. |
| `mock` | Deterministic fake model, for dry runs and tests. |

Per-model options (all optional):

- `api_key_env` — which environment variable holds the API key, for backends
  with their own key variables (e.g. `GEMINI_API_KEY` on an OpenAI-compatible
  endpoint). Providers default sensibly (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`).
- `timeout_s` — per-model request timeout (wired by the HTTP providers).
- `max_retries` — transient-failure retries for this model (attempts =
  retries + 1; default 2 retries).
- `max_concurrency` — cap on in-flight requests **for this model**.
- `requests_per_minute` — sliding-window rate limit for this model.

The last two exist because one global `--concurrency` is the wrong control as
soon as a matrix mixes a local model with a rate-limited hosted one. They
compose with the global setting: a call must satisfy the global semaphore,
this model's concurrency cap, and this model's rate limit.

```yaml
models:
  - id: claude-sonnet-5
    provider: anthropic
    max_concurrency: 4
    requests_per_minute: 50
  - id: local-llama            # unconstrained; runs as fast as it can
    provider: openai-compatible
    base_url: http://localhost:11434/v1
```

Retry backoff reacts to a 429 that already happened; a rate limit avoids
sending it.

API keys are never read from config files. They come from the environment or
from a gitignored secrets file — see [secrets.md](secrets.md).

## `variants`

Each variant is a named prompt. The prompt is either a path to an external
prompt file or inline messages:

```yaml
variants:
  - name: concise
    prompt: prompts/concise.yaml     # external file
  - name: detailed
    prompt:                          # inline messages
      - role: system
        content: Answer in full detail.
      - role: user
        content:
          - text: "{{ question }}"
          - image: "{{ files.photo }}"
```

Messages are multi-turn (`system` / `user` / `assistant`). A message's
`content` is either a plain string (shorthand for one text part) or a list of
typed parts: `text`, `image`, `file` (documents such as PDFs), `audio`. See
[prompts.md](prompts.md) for templating details.

## `cases`

Inline:

```yaml
cases:
  - id: dog-breed            # optional; defaults to the case's position
    vars: {question: "What breed is this dog?"}
    files: {photo: ./fixtures/dog1.jpg}
    expected: "Border Collie"   # optional, used by comparison scorers
    human_label: 5              # optional, used by agreement scorers
```

Or from a file:

```yaml
cases:
  file: cases.jsonl   # .jsonl or .csv
```

In CSV/JSONL datasets, a value of the form `file://path` marks a field as a
file reference.

### Cases from a source

For cases that live behind an API — too many to file, or too sensitive to
keep — point at Python you write instead:

```yaml
cases:
  source: sources/prod.py:make_source
  params: {region: eu}
  page_size: 200
  limit: 5000
```

The factory returns an object with `fetch(cursor, limit)`. Cases stream a page
at a time, so run size is bounded by concurrency rather than by case count.
Full reference: [no-look.md](no-look.md).

## `privacy`

```yaml
privacy:
  no_look: true          # drop prompts, outputs, judge rationales, attachments
  keep_case_ids: false   # default: ids are hashed, since ids identify records
```

For evaluating data nobody may read afterwards. Scores, counts, and timings
survive; everything derived from case content does not. See
[no-look.md](no-look.md).

## `scorecard`

Quality is a set of weighted criteria, each backed by a scorer:

```yaml
scorecard:
  - criterion: accuracy
    weight: 3
    scorer: {type: llm-judge, judge: quality-judge}
  - criterion: format
    weight: 1
    scorer: {type: json-schema, schema: schemas/answer.json}
```

`weight` must be positive (default 1). Scorer types: `exact`, `contains`,
`not-contains`, `regex`, `json-valid`, `json-schema`, `llm-judge`, `python`,
`agreement`. Keys other than `type` are parameters for that scorer — see
[scoring.md](scoring.md).

## `judges`

Named autoraters used by `llm-judge` scorers:

```yaml
judges:
  quality-judge:
    model: claude-sonnet-5          # must be an id from models
    rubric: prompts/judge-rubric.yaml   # path or inline messages
```

## `thresholds`

CI gates evaluated at the end of `evaling run`; failing any gate makes the run
exit non-zero:

```yaml
thresholds:
  min_pass_rate: 0.9       # 0..1
  min_score: 0.8           # aggregate scorecard score
  baseline: regression     # "regression" = compare against the pinned baseline;
                           # a run id compares against that specific run
```

## A complete config

Every block together, for reference:

```yaml
settings:
  output_dir: .evaling/runs
  cache_dir: .evaling/cache
  concurrency: 8
  cache: true

models:
  - id: claude-sonnet-5
    provider: anthropic
    params: {max_tokens: 1024}
    max_concurrency: 4
    requests_per_minute: 50
  - id: gpt-5.2
    provider: openai

variants:
  - name: concise
    prompt:
      - role: system
        content: Answer in one short sentence.
      - role: user
        content: "{{ question }}"
  - name: detailed
    prompt: prompts/detailed.yaml

cases:
  file: cases.jsonl

judges:
  quality:
    model: claude-sonnet-5
    rubric: prompts/judge-rubric.yaml

scorecard:
  - criterion: accuracy
    scorer: {type: contains}
    weight: 2
  - criterion: helpful
    scorer: {type: llm-judge, judge: quality}

thresholds:
  min_pass_rate: 0.9
  min_score: 0.8
  baseline: regression
```

## Settings resolution

Workspace settings merge across five layers; the most specific wins:

| Priority | Layer | Example |
|---|---|---|
| 1 (highest) | CLI flags | `--output-dir /data/runs` |
| 2 | Environment variables | `EVALING_OUTPUT_DIR=/data/runs` |
| 3 | `settings:` in the eval config | shared project defaults |
| 4 | User config `~/.config/evaling/config.yaml` | personal defaults |
| 5 | Built-in defaults | `.evaling/runs`, `.evaling/cache`, concurrency 8, cache on |

Environment variables:

| Variable | Setting | Type |
|---|---|---|
| `EVALING_OUTPUT_DIR` | `output_dir` | path |
| `EVALING_CACHE_DIR` | `cache_dir` | path |
| `EVALING_CONCURRENCY` | `concurrency` | integer |
| `EVALING_CACHE` | `cache` | boolean (`true/false/1/0/yes/no/on/off`) |

The user config file takes the same shape as the `settings:` block. A config
layer only overrides the fields it explicitly sets — e.g. a user config with
just `cache: false` changes nothing else.
