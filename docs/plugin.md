# Claude Code plugin

evaling ships as a [Claude Code](https://claude.com/claude-code) plugin. It
bundles the MCP server, a `/evaling:init` command, and a skill covering the
eval workflow and the `eval.yaml` reference — so an agent writes configs
against the real schema rather than guessing at it.

## Install

```sh
claude plugin marketplace add amro/evaling
claude plugin install evaling@evaling
```

Or from inside a session, `/plugin marketplace add amro/evaling` followed by
`/plugin install evaling@evaling`.

The only requirement is [uv](https://docs.astral.sh/uv/) on your PATH. The
plugin launches the server with `uvx --from 'evaling[mcp]' evaling mcp`, which
fetches the package on first use — there is nothing to install beforehand, and
nothing to keep in step with your project's virtualenv.

## What you get

| Component | What it does |
| --- | --- |
| MCP server | The seven tools in [mcp.md](mcp.md): `run_eval`, `get_run`, `get_case_result`, `compare_runs`, `list_runs`, `set_baseline`, `render_prompt` |
| `/evaling:init` | Scaffolds a project and drafts an `eval.yaml` for a task you describe |
| `running-evals` skill | The compare-against-baseline workflow, scorer selection, and the full config reference |

The skill loads only when a conversation calls for it. Idle cost is about 170
tokens per session.

## API keys

An MCP server is started with a minimal environment and does not inherit the
keys exported in your shell — see
[mcp.md](mcp.md#api-keys-dont-arrive-through-your-shell). Put them in
`.evaling.secrets.yaml` beside your `eval.yaml`, which the server reads for
itself and `evaling init` gitignores. See [secrets.md](secrets.md).

## Which config it uses

The server is launched from your session's working directory, so an `eval.yaml`
at the project root is found with no configuration. For a config elsewhere,
pass `config_path` to the tools that accept it.

## Versioning

The plugin carries the same version as evaling itself. Plugin 0.2.3 is
evaling 0.2.3, so the version you see in `claude plugin list` is the version
you are running, with no second number to reconcile.

What it *launches* is a compatible range rather than that exact version — a
plugin release for every patch would churn every install to no purpose. Both
the shared version and the range are checked by the test suite, so a release
that updates one and forgets the other fails CI.

## Installing the server without the plugin

The plugin is a convenience, not a requirement. To wire the MCP server up
directly — including for clients other than Claude Code — see
[mcp.md](mcp.md).
