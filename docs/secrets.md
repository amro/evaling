# Secrets

API keys never belong in `eval.yaml`. That file is meant to be committed — it
*should* be, since a run is only reproducible if the config is pinned — and a
key committed once is a key that has to be rotated.

evaling therefore refuses to read keys from config files at all. There are
exactly two places it will look: the environment, and a secrets file that is
gitignored by default.

## The lookup order

For each key, the first hit wins:

1. **The real environment** — `ANTHROPIC_API_KEY=... evaling run`
2. **`$EVALING_SECRETS`** — an explicit path to a secrets file
3. **`.evaling.secrets.yaml`** — beside your `eval.yaml`
4. **`~/.config/evaling/secrets.yaml`** — your personal fallback

The environment winning outright is the important part: CI injects secrets the
usual way, and a developer can override one key for one command without
touching a file.

## The project secrets file

`evaling init` writes `.evaling.secrets.yaml.example` and a `.gitignore` that
already excludes the real thing. Copy it and fill it in:

```sh
cp .evaling.secrets.yaml.example .evaling.secrets.yaml
chmod 600 .evaling.secrets.yaml
```

```yaml
# .evaling.secrets.yaml
ANTHROPIC_API_KEY: sk-ant-...
OPENAI_API_KEY: sk-...
GEMINI_API_KEY: ...
```

It's a flat mapping of environment-variable name to value. Nothing else — no
nesting, no interpolation. Scalars are read as strings; a nested mapping or
list is rejected as a config error rather than silently stringified. A key
with no value is skipped, so you can leave a placeholder line in place.

## A personal file across projects

If you work in several eval repos, put your keys once in
`~/.config/evaling/secrets.yaml` and skip the per-project file. The project
file wins where both define the same key, so a repo can still pin a different
account.

## Pointing at a file elsewhere

```sh
EVALING_SECRETS=~/keys/work.yaml evaling run
```

Useful for switching between accounts, or for pulling secrets from a mount
that your secret manager populates. Unlike the default locations, a path you
name explicitly must exist — pointing `EVALING_SECRETS` at a missing file is
an error, not a silent no-op, because the alternative is a confusing
"missing API key" much later.

## What evaling does to protect you

**Permissions are checked.** If the secrets file is readable by group or
others, the run prints a warning naming the file. It doesn't refuse to run —
your homedir may legitimately be locked down at a higher level — but you get
told. The check is POSIX-only; Windows reports synthesized mode bits that say
nothing about the real ACL, so it is skipped there rather than warning on
every run.

**Secrets never enter the process environment.** Loaded values are passed
explicitly to the provider that needs them. They are not written into
`os.environ`, so nothing else in the process — a Python scorer, a subprocess,
a library that logs its environment — can read them incidentally.

**The `command` provider is the exception, deliberately.** It launches a
program, and a program reads credentials from its environment. That provider
receives the secrets in its subprocess environment because it cannot work
otherwise; that inheritance stops at the process evaling spawns.

**Values are redacted from error output.** If a provider echoes a key back in
an error message, or a URL contains one, evaling replaces the value with
`<redacted>` before printing or storing it — run artifacts on disk included.

## Which key a model uses

By default, each provider reads its conventional variable:

| Provider | Variable |
| --- | --- |
| `anthropic` | `ANTHROPIC_API_KEY` |
| `openai` | `OPENAI_API_KEY` |
| `openai-compatible` | none by default — set `api_key_env` if the endpoint needs one |
| `command` | none (the program's own concern) |
| `mock` | none |

`openai-compatible` sends no key unless you name one, which is what makes
local models work with no setup. Set `api_key_env` per model — necessary when
a matrix hits two endpoints that both speak the OpenAI protocol:

```yaml
models:
  - id: gemini-3.1-pro-preview
    provider: openai-compatible
    base_url: https://generativelanguage.googleapis.com/v1beta/openai
    api_key_env: GEMINI_API_KEY

  - id: llama3.1:8b
    provider: openai-compatible
    base_url: http://localhost:11434/v1     # local; needs no key at all
```

## If a key is missing

Every cell on that model fails, naming both the model and the variable it
expected. The run itself finishes and reports them — nothing checks keys
before the first call, so a config with one good model and one bad still gets
you the good model's results:

```
model 'claude-sonnet-5': no API key found — set ANTHROPIC_API_KEY
```

It does not fall back to an unauthenticated request or to a different key.

If you are seeing this **through the MCP server** while `evaling run` works
from the same directory, the environment is the reason: an MCP client starts
the server with a minimal one that does not carry your key. Use the project
secrets file, which the server reads for itself — see
[mcp.md](mcp.md#api-keys-dont-arrive-through-your-shell).

## CI

Use your CI system's secret store and let it set the environment — no secrets
file, no config change:

```yaml
- run: evaling run --max-cost 5.00
  env:
    ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

See [ci.md](ci.md) for full pipelines.
