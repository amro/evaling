# MCP server

`evaling mcp` exposes evaling's operations as [MCP](https://modelcontextprotocol.io)
tools over stdio, so an agent can drive the whole loop: edit a prompt, run the
eval, read the scores, drill into what failed, iterate.

CI does **not** need this — use the CLI there (exit codes plus `--json` or
`--html`). MCP is for interactive, agent-driven iteration.

## Install

The MCP SDK is an optional extra, so CLI-only users don't carry it:

```sh
pip install 'evaling[mcp]'      # or: uv tool install 'evaling[mcp]'
```

Without it, `evaling mcp` exits with that install hint.

## Connect

Register evaling as an MCP server in your client. For Claude Code:

```sh
claude mcp add evaling -- evaling mcp
```

Or by config, in any MCP client:

```json
{
  "mcpServers": {
    "evaling": {
      "command": "evaling",
      "args": ["mcp"]
    }
  }
}
```

Run it from your eval project directory — relative `config_path` values and the
output directory resolve exactly as they do for the CLI, so the agent sees the
same runs you see.

## Tools

| Tool | Purpose |
|---|---|
| `run_eval` | Run the matrix to completion and return the summary |
| `get_run` | Re-read a stored run: `summary`, `failures`, or `full` (paginated) |
| `get_case_result` | One cell in full — untruncated output, criteria, prompt sent |
| `compare_runs` | Score and pass-rate deltas between two runs |
| `list_runs` | Stored runs, newest first, with the pinned baseline |
| `set_baseline` | Pin a run as the regression-gating baseline |
| `render_prompt` | Render a case's prompt, or validate the whole config — no model calls |

Run references work as in the CLI: a run id, a label, `latest`, or `baseline`.

### `run_eval`

```json
{"config_path": "eval.yaml", "variants": ["concise"], "max_cost_usd": 1.0}
```

Blocking: it returns when the run finishes, emitting MCP progress
notifications as cells complete. Optional `models`, `variants`, `cases` narrow
the matrix; `label` names the run; `no_cache` bypasses the cache;
`max_cost_usd` caps spend.

It returns the run id, counts, totals, the aggregate matrix, the gate verdict,
and up to five failing cells — not the full records. Ask for the rest with
`get_run`.

### `get_run`

`detail="summary"` (default) returns aggregates only. `detail="failures"`
returns just the failing cells with their reasons. `detail="full"` returns
every cell, 20 per page — pass `page` to walk them, and check
`page.has_more`.

### `render_prompt`

With `variant` and `case_id`, returns the exact messages that case renders to.
With neither, validates the entire config and reports any render errors. Either
way no model is called, which makes it the cheap way to check an edit before
spending anything.

## Argument names must match exactly

Tool arguments are validated against the advertised schema, so a misspelled or
unknown argument is refused:

```
Input validation error: Additional properties are not allowed ('config' was unexpected)
```

That matters most for `run_eval`, which spends money: without validation a
typo'd `config_path` would have run the *default* config and reported success.
Types are checked too, so send `2` rather than `"2"` for an integer. Take
parameter names from `list_tools` rather than guessing — `run_eval` takes
`config_path`, not `config`.

## Design notes

**Responses are written for a model to read.** Summaries by default, drill-down
on request, pagination on anything unbounded, and long outputs snipped with an
explicit pointer to `get_case_result` — so a 40-cell run doesn't dump 200 KB
into an agent's context.

**The server holds no logic of its own.** Every tool is a thin call into the
same core library the CLI uses, so both surfaces behave identically and can't
drift.

## A typical loop

1. `render_prompt` — check the config renders after an edit.
2. `run_eval` — run it; read the matrix and the first failures.
3. `get_run(detail="failures")` — see everything that broke, and why.
4. `get_case_result` — inspect one cell's full output and prompt.
5. Edit the prompt, re-run, then `compare_runs` to confirm the change helped.
6. `set_baseline` once you're happy, so later runs gate against it.
