# eval.yaml reference

Complete set of legal keys. Unknown keys are rejected at load time everywhere
except scorer parameters, so a typo is a load error rather than a silent
no-op. Relative paths resolve against the directory holding the config file.

The MCP server serves a generated JSON Schema at `evaling://config-schema`.
Prefer it for key names, types and enums: it comes from the installed evaling,
so it cannot be out of date. It stops there. Cross-field rules — noted below
and enforced at load time — are not expressible in JSON Schema, so a config
that validates against it can still be rejected. `evaling validate` is the
check that covers both.

## Top-level keys

| Key | Required | Purpose |
| --- | --- | --- |
| `settings` | no | Workspace concerns: output and cache directories, concurrency. |
| `models` | yes | The systems under test, and any judge models. At least one. |
| `variants` | yes | The prompts being compared. At least one; two or more to compare. |
| `cases` | yes | The inputs to run against — inline, from a file, or from a source. |
| `scorecard` | yes | Weighted criteria that grade each cell. At least one. |
| `judges` | no | Named autoraters referenced by `llm-judge` scorers. |
| `thresholds` | no | Gates that decide `evaling run`'s exit code. |
| `privacy` | no | No-look mode, for data that must not be readable afterwards. |

### settings

| Key | Default | Meaning |
| --- | --- | --- |
| `output_dir` | `.evaling/runs` | Where runs are stored. |
| `cache_dir` | `.evaling/cache` | Where response caching lives. |
| `concurrency` | `8` | Cells in flight across the whole run. |
| `cache` | `true` | Reuse identical prior responses instead of re-calling. |

### models

A list. Each entry:

| Key | Meaning |
| --- | --- |
| `id` | Model identifier, sent to the provider and shown in results. |
| `provider` | One of the providers below. |
| `base_url` | Endpoint override, for `openai-compatible`. |
| `command` | The program to run. Required by, and only valid for, `command`. |
| `api_key_env` | Environment variable holding the key. Must look like a variable name — a pasted key is rejected here rather than committed. |
| `timeout_s` | Per-request timeout. Defaults to 120s, or 300s for `command`. |
| `max_retries` | Retries for transient failures; attempts are retries + 1. |
| `max_concurrency` | Cap in-flight calls to this model. |
| `requests_per_minute` | Proactive rate limit for this model. |
| `role` | `candidate` (default, gets matrix cells), `judge` (only called by `llm-judge`), or `both`. |
| `params` | Provider parameters — `max_tokens`, `temperature`, and `pricing` for cost overrides. |

### variants

A list of `{name, prompt}`. `prompt` is either a path to a prompt file or an
inline list of messages. Each message is `{role, content}` with `role` one of
`system`, `user`, `assistant`. `content` is a string — shorthand for a single
text part — or a list of parts, each exactly one of `text`, `image`, `file`,
`audio`, `video`. Case variables interpolate as `{{ name }}`.

### cases

Three forms:

- **Inline** — a list of case objects.
- **File** — `{file: cases.jsonl}`.
- **Source** — `{source: "path/to/file.py:factory", params: {}, page_size: 100,
  limit: null}`, for cases fetched from user code a page at a time.

A case object:

| Key | Meaning |
| --- | --- |
| `id` | Identifier. Generated if omitted. |
| `vars` | Values interpolated into the prompt. |
| `files` | Named attachment paths. |
| `expected` | The correct answer. Comparison scorers default to this. |
| `human_label` | Ground truth for `agreement` scorers in judge calibration. |

### scorecard

A list of `{criterion, weight, scorer}`. `criterion` is a unique name, `weight`
defaults to `1.0` and must be positive, and `scorer` is `{type, ...params}`.

### judges

A mapping of name to `{model, rubric}`. `rubric` is a prompt file path or an
inline message list. The judge must return JSON carrying a numeric verdict.

### thresholds

| Key | Meaning |
| --- | --- |
| `min_pass_rate` | Minimum overall pass rate, 0–1. |
| `min_score` | Minimum overall weighted score, 0–1. |
| `baseline` | `regression` gates against the pinned baseline; a run id gates against that run. |

Below a gate, `evaling run` exits non-zero. Gates apply to the run overall, not
per variant.

### privacy

| Key | Default | Meaning |
| --- | --- | --- |
| `no_look` | `false` | Drop prompts, outputs, rationales and attachments before anything is written or shown, leaving scores and timings. |
| `keep_case_ids` | `false` | Keep raw case ids in no-look mode instead of hashing them. |

## Providers

| Provider | Key | Notes |
| --- | --- | --- |
| `anthropic` | `ANTHROPIC_API_KEY` | First-party Claude API. |
| `openai` | `OPENAI_API_KEY` | First-party OpenAI API. |
| `openai-compatible` | none unless `api_key_env` is set | Any endpoint speaking the OpenAI protocol: Gemini, OpenRouter, vLLM, Ollama. Requires `base_url`. |
| `command` | none | Runs a local program. Offline, deterministic, no key. |
| `mock` | none | Fixed string or echo. For config smoke tests only. |

The `command` contract: `command` is a single shell string, run with the
working directory set to the config's directory. The rendered request arrives
on stdin as JSON — `messages[]` each with `role` and `parts[]`, every part
carrying a `type`, a text part being `{"type": "text", "text": "..."}` — and
the completion is read from stdout. A non-zero exit becomes a cell error.

## Scorer types

| Type | Parameters | Behaviour |
| --- | --- | --- |
| `exact` | `value` (default: the case's `expected`), `strip` (`true`), `case_sensitive` (`true`) | Output equals the expected value. |
| `contains` | `value` (default: `expected`), `case_sensitive` (`true`) | Output contains the value. |
| `not-contains` | same as `contains` | Output does not contain the value. |
| `regex` | `pattern` (required), `case_sensitive` (`true`) | Output matches the pattern. |
| `json-valid` | none | Output parses as JSON. |
| `json-schema` | `schema` (required) | Output parses as JSON and validates against the schema. |
| `llm-judge` | `judge` (required, a name from `judges`), `scale` (`1.0`), `pass_at` (`0.5`) | An autorater grades the output against a rubric. Billable: one model call per cell. |
| `python` | `file` (required), `function` (`score`), `pass_at` (`1.0`) | Your own scoring function. |
| `agreement` | `mode` (`exact` or `within`), `tolerance` (`0`), `field` (`score`) | How well a judge's verdict matches the case's `human_label`. For calibrating judges. |

## Cross-field rules

Checked at load time, and not expressible in JSON Schema — a config can satisfy
every table above and still be rejected for one of these:

- `openai-compatible` requires `base_url`; `command` requires `command`, and
  `command` is invalid on any other provider.
- `params.pricing` must carry finite, non-negative numeric `input` and
  `output`. Validated before the run, because a bad rate would otherwise
  surface after calls were billed.
- Model ids, variant names and criterion names must each be unique.
- An inline `cases` list must not be empty.
- At least one model must have role `candidate` or `both`.
- A judge must reference a model that exists, and that model must not be a
  plain `candidate`. A model declared `judge` or `both` must actually be used
  by some judge.
- An `llm-judge` criterion must name a `judge` that exists.

`evaling validate` reports these without calling a model.

### The python scorer contract

The function is named `score` unless `function` says otherwise, receives
`(output: str, case: dict)`, and may be sync or async. The case mapping carries
`id`, `vars`, `files`, `expected` and `human_label`.

It may return:

- a **bool** — pass or fail;
- a **number in [0, 1]** — the score, passing at `pass_at`;
- a **mapping** with `score`, and optionally `passed` (a bool) and `detail` (a
  string shown in results).

Anything else, or a number outside [0, 1], is a scoring error.
