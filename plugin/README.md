# evaling plugin for Claude Code

Runs [evaling](https://github.com/amro/evaling) from Claude Code: compare
prompt variants and models, score the results, and gate on regressions against
a pinned baseline.

## What it provides

- **The evaling MCP server**, launched with `uvx` — no prior install needed.
  Seven tools: `run_eval`, `get_run`, `get_case_result`, `compare_runs`,
  `list_runs`, `set_baseline`, `render_prompt`.
- **`/evaling:init`** — scaffold a project and author an `eval.yaml` for a
  task you describe.
- **The `running-evals` skill** — the workflow, scorer selection, and a
  complete `eval.yaml` reference, loaded when the conversation calls for it.

## Requirements

[uv](https://docs.astral.sh/uv/) on your PATH. The server runs via
`uvx --from 'evaling[mcp]' evaling mcp`, which fetches the package on first
use; nothing else needs installing.

## API keys

An MCP server is started with a minimal environment and does not inherit the
keys exported in your shell. Put them in `.evaling.secrets.yaml` beside your
`eval.yaml` — the server reads that file itself, and `evaling init` gitignores
it.

## Config discovery

The server is launched from your session's working directory, so an `eval.yaml`
at the project root is found without configuration. For a config elsewhere,
pass `config_path` to the tools that take it.

## Documentation

Full documentation is at
[github.com/amro/evaling](https://github.com/amro/evaling/tree/main/docs).
