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

evaling needs **`mcp` 2.0 or newer**. The 1.x line renamed and moved the
classes the server is built on, so it cannot serve both; on 1.x, `evaling mcp`
says so and names the version it found rather than repeating the install hint
above. Upgrade with `pip install --upgrade 'evaling[mcp]'`.

## Connect

### Claude Code

```sh
cd /path/to/your/eval-project
claude mcp add evaling -- evaling mcp
```

Added from the project directory, so the server starts there.

### Claude Desktop

`claude_desktop_config.json` — macOS:
`~/Library/Application Support/Claude/claude_desktop_config.json`, Windows:
`%APPDATA%\Claude\claude_desktop_config.json`.

```json
{
  "mcpServers": {
    "evaling": {
      "command": "evaling",
      "args": ["-c", "/absolute/path/to/eval.yaml", "mcp"]
    }
  }
}
```

### Cursor

`.cursor/mcp.json` in the project, or `~/.cursor/mcp.json` globally:

```json
{
  "mcpServers": {
    "evaling": {
      "command": "evaling",
      "args": ["-c", "/absolute/path/to/eval.yaml", "mcp"]
    }
  }
}
```

### Any other MCP client

```json
{
  "mcpServers": {
    "evaling": {
      "command": "evaling",
      "args": ["mcp"],
      "cwd": "/absolute/path/to/your/eval-project"
    }
  }
}
```

### Use absolute paths, or set the working directory

A desktop app launches the server from wherever the app happens to be, not
from your project — so `evaling mcp` with no arguments looks for `eval.yaml`
in the wrong place. The server starts normally and every tool that needs the
config then fails with `config file not found: eval.yaml`, which reads as a
broken server rather than a misplaced one.

Either set `cwd` if your client supports it, or pass an absolute `-c`. With an
absolute `-c`, runs land beside that config (see
[configuration.md](configuration.md#where-a-relative-directory-points)), so the
agent and your own `evaling list` see the same history.

If `evaling` isn't on the PATH your client sees — common with a tool installed
into a user directory — give the full path as `command`. `evaling doctor`
prints it on the `evaling` line; `which evaling` (`where evaling` on Windows)
also works.

Note that doctor's `python` line is the interpreter, not the launcher. If you
would rather use that, the module entry point takes the same arguments:
`"command": "/path/to/python", "args": ["-m", "evaling", "mcp"]`.

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

Responses are shaped for a model to parse, so they are not identical to the
CLI's `--json`. The one that catches people out is the run listing:

| | `evaling --json list` | `list_runs` |
|---|---|---|
| Shape | a bare array of runs | `{"runs": [...], "total": N, "baseline": ...}` |
| Run id key | `id` | `run_id` |
| Contents | the full stored metadata per run | the six fields worth summarizing |

So a script written against one does not read the other. The CLI form is the
raw record; the MCP form is a summary with the total and the pinned baseline
alongside it, because an agent asking "what runs are there" almost always
wants those two next.

### `run_eval`

```json
{"config_path": "eval.yaml", "variants": ["concise"], "max_cost_usd": 1.0}
```

Blocking: it returns when the run finishes, emitting MCP progress
notifications as cells complete. Optional `models`, `variants`, `cases` narrow
the matrix; `label` names the run; `no_cache` bypasses the cache;
`max_cost_usd` caps spend; `fail_fast` stops at the first failing cell and
sets `stopped_early` in the response.

`sample` runs a random N of the selected cases — the cheap way to check
whether a prompt edit helped before paying for the whole matrix. The response
carries a `selection` object with the seed that produced the draw; pass it
back as `sample_seed` to compare two prompts over exactly the same cases:

```json
{"config_path": "eval.yaml", "sample": 20}
→ {"selection": {"sample": 20, "seed": 2894127714, "available": 2000}, ...}

{"config_path": "eval.yaml", "sample": 20, "sample_seed": 2894127714}
```

Without the seed the second run draws different cases, so any difference in
score is partly the cases and partly the prompt.

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

Refused under [no-look mode](no-look.md): rendering a case is reading it, so
there is nothing this tool could usefully return.

## Argument names must match exactly

An argument a tool doesn't declare is refused, and the error names what it does
take:

```
unknown argument(s) for run_eval: config. This tool takes: cases, config_path,
fail_fast, label, max_cost_usd, models, no_cache, sample, sample_seed,
variants.
```

That matters most for `run_eval`, which spends money: without the check a
typo'd `config_path` ran the *default* config and reported success. Only
argument *names* are checked — list and object arguments sent as JSON-encoded
strings still work, since some clients send them that way.

### Source-backed configs

`cases` cannot narrow a run whose cases come from a
[case source](large-datasets.md): they're fetched lazily, so the ids aren't
known in advance. Filter inside your source instead. Such a run also needs a
bound — `limit` in the config or `max_cost_usd` on the call — since otherwise
the number of model calls is whatever the source returns.

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
