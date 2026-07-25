# Troubleshooting

Symptoms first, in roughly the order people hit them.

## Config and startup

### `no API key found — set ANTHROPIC_API_KEY`

evaling found no key for that model. It checks the real environment first,
then `$EVALING_SECRETS`, then `.evaling.secrets.yaml` beside your config, then
`~/.config/evaling/secrets.yaml`. Keys are never read from `eval.yaml`.

```sh
echo $ANTHROPIC_API_KEY | cut -c1-6      # is it actually set in this shell?
ls -l .evaling.secrets.yaml              # is the file where evaling looks?
```

If the endpoint expects a different variable — Gemini, OpenRouter, an internal
gateway — name it on the model with `api_key_env`. See [secrets.md](secrets.md).

### `'question' is undefined`

A prompt template referenced a variable no case supplies. Run `evaling
validate`: it renders every prompt against every case and reports each
mismatch, without calling a model.

The usual causes are a typo, or the shape difference between inline and
dataset cases — inline cases nest variables under `vars:`, while dataset rows
put them at the top level.

### `extra fields not permitted`

The config schema rejects unknown keys on purpose, so a misspelled field fails
loudly instead of being silently ignored. The message names the offending key
and where it appeared. Check [configuration.md](configuration.md) for the
field list.

### `unsupported case file type '.json'`

Datasets are `.jsonl` (one JSON object per line) or `.csv`. A regular `.json`
array isn't accepted; convert it:

```sh
python -c "import json,sys;[print(json.dumps(r)) for r in json.load(open('cases.json'))]" > cases.jsonl
```

### `case file not found` for an attachment that exists

Attachment paths in a dataset resolve relative to **the dataset file**, not to
your working directory or to `eval.yaml`. Inline case paths resolve relative
to the config. Move the file, or adjust the path to match the file declaring
it.

## During a run

### The run stops early and reports skipped cells

`--max-cost` reached its ceiling. This is admission control, not a failure:
evaling stops issuing new calls and tells you how many cells it skipped.
Raise the ceiling or narrow the matrix with `--model` / `--variant` / `--case`.

### `model 'x' does not support media type 'video'`

The capability check fired before sending, which is deliberate — an
unsupported attachment would otherwise come back as an opaque provider error
after you'd paid for the request. Either drop the attachment for that model or
drop the model from the matrix.

### 429s, or the provider throttles the whole matrix

Cap the model rather than the run:

```yaml
models:
  - id: claude-sonnet-5
    provider: anthropic
    max_concurrency: 4
    requests_per_minute: 50
```

Per-model limits compose with the global `--concurrency`, so a rate-limited
hosted model no longer holds back a fast local one. evaling also backs off and
retries on 429 and 5xx, but a limit avoids the throttle instead of reacting to
it.

### A run is slower than the concurrency setting suggests

Check whether one model in the matrix carries `max_concurrency` or
`requests_per_minute` — those are per-model caps and the matrix can only go as
fast as the slowest constrained model allows. `--concurrency` is an upper
bound on the whole run, not a guarantee.

### The run died and I don't want to pay for it twice

Every result is written as it completes, so nothing finished is lost:

```sh
evaling list                  # the interrupted run shows status "running"
evaling run --resume <run-id>
```

Resume re-runs only the cells with no result, and refuses if the config
changed in a way that would make the halves inconsistent. A torn final line
from a hard kill is detected and discarded.

### `config does not match run <id>`

Resume compares a fingerprint of the whole config, including the prompt,
case, and attachment files it references — so editing a prompt file counts
even though `eval.yaml` itself is untouched. Half a run under one config and
half under another isn't a run you could draw a conclusion from, so evaling
refuses rather than silently mixing them. Revert the edit and resume, or
start a fresh run.

Resuming a run that already finished is likewise an error
(`already complete; nothing to resume`) rather than a no-op.

## Results

### A cell shows an error instead of a score

`evaling show latest --failures` prints the reason for each. Provider errors
carry the HTTP status and the provider's message (with secrets redacted).
Scorer errors name the criterion that raised.

### The cache isn't hitting when I expect it to

The key covers only what changes the answer: provider, model, base URL,
request parameters, and the rendered messages. Labels, output directory, and
concurrency don't invalidate it — but any edit to a prompt or a case's
variables does, because that changes the request.

```sh
evaling cache info      # location and entry count
```

Note that `--no-cache` bypasses both reads and writes for that run.

### Scores look wrong

Drill into a single case to see exactly what was sent and returned:

```sh
evaling show latest --case sky
```

For a judge, the stored `rationale` tells you what the judge thought it was
doing — usually the fastest way to spot a rubric that's grading something
other than what you meant. If a judge disagrees with you systematically,
calibrate it before trusting it: [evaluating-judges.md](evaluating-judges.md).

### `result.records` is empty but the run clearly worked

Above 10,000 cells a run stops holding every record in memory;
`records_truncated` is set and `records` is empty. It's empty rather than
partial deliberately — a partial list would have silently given you wrong
numbers. Aggregates, counts, and totals are complete and unaffected. Stream
the records instead:

```python
for record in result.iter_records():
    ...
```

## Reports and output

### The HTML report's "failures only" toggle does nothing

The report contains no JavaScript by design — filtering is a CSS checkbox, so
it works in any browser but not in a text-mode viewer or an email client that
strips `<style>`. Open the file in a real browser.

### The HTML report says "Large run — showing partial detail"

Above 2,000 cells the drill-down covers failing cases only. The summary matrix
and gate still describe the whole run. A full drill-down is about 1.5 KB of
HTML per cell, so an unbounded report of a large run is a file a browser won't
open. For complete data use `evaling export <run> --format csv`, or
`evaling show <run> --case <id>` for a single case.

### Terminal output is mangled, or colors leak into a log

```sh
evaling --no-color run       # plain text
evaling --json run           # machine-readable, for scripts
evaling --quiet run          # errors and essentials only
```

### Non-ASCII characters render as mojibake on Windows

evaling reads and writes every file as UTF-8 regardless of platform. If output
in a terminal looks wrong, that's the console code page rather than the file:

```powershell
chcp 65001
```

## Environment

### `evaling: command not found` after installing

The tool directory isn't on your `PATH`. Either fix that (`uv tool update-shell`)
or run the module directly:

```sh
python -m evaling --version
```

### MCP server: `evaling mcp` exits immediately

The MCP extra isn't installed:

```sh
uv tool install "evaling[mcp]"
```

The server speaks stdio and is meant to be launched by an MCP client, not run
interactively — started by hand it will look like it's hanging. See
[mcp.md](mcp.md).

## Still stuck

Useful things to include in a bug report:

```sh
evaling --version
evaling validate            # does the config itself check out?
evaling --json show latest  # the full run record
```

The run directory under `.evaling/runs/<id>/` holds the config snapshot,
per-cell results, and the gate outcome — it's usually enough to reproduce a
problem without your API keys.
