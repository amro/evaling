# Providers

A provider is how evaling turns a rendered prompt into a model response. Five
ship built in; the interface is small, so adding more is contained work.

| Provider | Talks to | Key |
|---|---|---|
| `anthropic` | Anthropic Messages API | `ANTHROPIC_API_KEY` |
| `openai` | OpenAI chat completions | `OPENAI_API_KEY` |
| `openai-compatible` | Any OpenAI-format endpoint (`base_url` required) | optional |
| `command` | Any CLI or script | — |
| `mock` | Built-in deterministic fake | — |

API keys come from environment variables only — never from config files.

## `anthropic`

```yaml
models:
  - id: claude-sonnet-5           # also the API model name
    provider: anthropic
    params: {max_tokens: 1024}
```

System messages are hoisted into the API's top-level `system` field
automatically. Images are sent as image blocks and PDFs as document blocks;
audio and video are rejected at validation time. `max_tokens` defaults to 4096
if you don't set it (the API requires it).

If the model declines a request, evaling records a clear per-cell error naming
the refusal category rather than an empty output that would silently score
zero.

## `openai`

```yaml
models:
  - id: gpt-5.2
    provider: openai
    params: {temperature: 0.2}
```

Images, audio, and PDFs are supported. Text-only turns are sent as plain string
content (rather than a content-parts array) for the widest server
compatibility.

## `openai-compatible`

The same wire format as `openai`, pointed anywhere. This one adapter covers
most of the ecosystem — Ollama, vLLM, LM Studio, llama.cpp's server,
OpenRouter, and Google's Gemini OpenAI-compatibility endpoint.

```yaml
models:
  # Local — no key needed
  - id: llama3.1:8b
    provider: openai-compatible
    base_url: http://localhost:11434/v1     # Ollama

  # Gemini via its OpenAI-compatible endpoint
  - id: gemini-2.5-flash
    provider: openai-compatible
    base_url: https://generativelanguage.googleapis.com/v1beta/openai
    api_key_env: GEMINI_API_KEY

  # OpenRouter
  - id: anthropic/claude-sonnet-5
    provider: openai-compatible
    base_url: https://openrouter.ai/api/v1
    api_key_env: OPENROUTER_API_KEY
```

An API key is sent only when `api_key_env` names a variable that is set, so
local servers work with no key at all.

**Gemma** runs here too: locally via Ollama (`ollama pull gemma3`) or hosted
through the Gemini endpoint above.

## `command`

Turns any executable into an evaluable model: the request is written to stdin
as JSON, and stdout is the response.

```yaml
models:
  - id: my-agent
    provider: command
    command: ./run-agent.sh
    timeout_s: 600
```

`command` runs through a shell, so pipes and redirection work. It is a static
config value — no case or template data is ever interpolated into it — but
treat it like any other command you commit to a repo.

The stdin payload is `{"model": ..., "params": {...}, "messages": [...]}`, where
each message has `role` and `parts`; media parts carry their resolved path,
media type, and content hash so your script can read the files.

stdout is used verbatim as the response — unless it's a JSON object with a
`text` key, which lets a script report usage too:

```json
{"text": "the answer", "input_tokens": 120, "output_tokens": 45, "cost_usd": 0.002}
```

`input_tokens` and `output_tokens` must be whole numbers and `cost_usd` a
number (numeric strings are accepted); anything else fails that cell with a clear error rather
than corrupting the run's totals. A non-zero exit becomes a retryable error
carrying stderr. Every media kind is allowed, since only your script knows what
it can handle.

## `mock`

The built-in deterministic fake. Echoes the last user message (with
`[image:a1b2c3d4]`-style markers for media) or returns a fixed `response`
param. It powers the test suite, `evaling init`, and offline dry runs.

Params: `response`, `fail_times`, `error: fatal`, `cost`.

## Per-model options

| Option | Effect |
|---|---|
| `api_key_env` | Environment variable holding the key (defaults per provider) |
| `base_url` | Override the endpoint (required for `openai-compatible`) |
| `timeout_s` | Per-request timeout (default 120s HTTP, 300s command) |
| `max_retries` | Retries for transient failures (attempts = retries + 1) |
| `params.model` | API model name, when it differs from the config `id` |
| `params.pricing` | `{input: <usd per Mtok>, output: <usd per Mtok>}` |

Everything else under `params` is forwarded to the provider's API verbatim
(`temperature`, `max_tokens`, `top_p`, …).

Forwarded verbatim means evaling does not validate them — which parameters a
model accepts is the vendor's business, and it changes. A parameter a model no
longer supports comes back as a provider error naming it, at which point the
vendor's current API reference is the place to look. `temperature`, for
instance, is not accepted by every current Claude model.

## Cost tracking

evaling ships published per-model rates for Anthropic models and computes cost
from reported token usage. Anything else — OpenAI models, local servers,
arbitrary endpoints — reports **no cost** rather than a guess, because the
operator sets those rates. Supply your own to get cost tracking anywhere:

```yaml
models:
  - id: gpt-5.2
    provider: openai
    params:
      pricing: {input: 1.25, output: 10.0}   # USD per million tokens
```

A config `pricing` block always wins over the built-in table, so you can
correct a stale rate without waiting for a release. Rates must be non-negative
numbers and are validated when the config loads, before any spend.

If a priced model's endpoint reports no token usage at all, cost is recorded as
unknown rather than `$0` — calling it free would silently under-count spend
against `--max-cost`.

## Errors and retries

Failures map to two classes:

- **Retryable** — 408/409/429, any 5xx, timeouts, connection errors, and
  non-zero exits from `command`. The engine retries these with exponential
  backoff (`max_retries`, default 2 retries). A numeric `Retry-After` header is
  honored instead of the guessed backoff, capped at 60s so a run can't hang.
- **Fatal** — other 4xx, unparseable responses, missing API keys, refusals.
  Recorded on the cell immediately; no retry.

API keys are redacted from error messages before they reach the terminal or
`results.jsonl`, in case an upstream gateway reflects request headers.

Either way the failure is isolated to its cell: the run continues and reports
the error in the summary.

### Working directory

The script runs in the directory containing the config, so relative paths in
`command:` mean the same thing as relative paths everywhere else in that
config:

```yaml
models:
  - id: my-agent
    provider: command
    command: python3 agents/run.py      # relative to eval.yaml, not to your shell
```

Secrets reach the script through its environment — a wrapper around a real API
usually needs the same key evaling would have used. See
[secrets.md](secrets.md).

## Adding a provider

Subclass `Provider` (or `HttpProvider` for HTTP APIs), declare which media kinds
it accepts, implement `complete()`, and register it:

```python
from evaling.providers.http import HttpProvider


class MyProvider(HttpProvider):
    DEFAULT_API_KEY_ENV = "MY_API_KEY"
    SUPPORTED_MEDIA = frozenset({"image"})

    async def complete(self, request):
        # Build the payload, call self.post_json(...), return a Completion.
        ...
```

`SUPPORTED_MEDIA` is enforced before any model call, so an unsupported part
type fails at validation/dry-run time instead of mid-run. `HttpProvider` gives
you client lifecycle, key resolution, timeouts, and error mapping; raise
`ProviderError(msg, retryable=...)` for anything it doesn't cover.

## When a provider misbehaves

```sh
evaling run --sample 3 --log-requests trace.jsonl
```

One JSON object per call — the request body sent, the response received, the
status, and the elapsed time — so you can see what the provider actually got
rather than what you meant to send. For the `command` provider it records the
script's exit code, stdout, and stderr, which is otherwise invisible.

Headers are never recorded, so the file cannot contain your API key; see
[cli.md](cli.md#debugging-a-provider).
