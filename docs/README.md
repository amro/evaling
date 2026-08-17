# evaling documentation

## Start here

- **[Tutorial](tutorial.md)** — the full walkthrough: install, first offline
  eval, real providers, datasets, judges, comparing runs, CI gating. Start
  here if you're new.
- [Getting started](getting-started.md) — the short version.
- [Examples](../examples/) — seven complete, runnable evals: text and
  multimodal, a RAG pipeline behind the `command` provider, and a no-look run
  over a paging source. All exercised by the test suite.

## Using evaling

| Page | What's in it |
| --- | --- |
| [CLI reference](cli.md) | Every command, flag, run reference, and exit code |
| [Configuration](configuration.md) | The `eval.yaml` reference and settings layering |
| [Prompts](prompts.md) | Variants, Jinja2 templating, multi-turn, media, datasets |
| [Scoring](scoring.md) | Scorers, scorecards, LLM judges, thresholds |
| [Providers](providers.md) | Anthropic, OpenAI, OpenAI-compatible, local models, `command` |
| [Secrets](secrets.md) | Where API keys come from, and how they're protected |
| [Large datasets](large-datasets.md) | Case sources: streaming hundreds of thousands of cases from your own API |
| [Storage](storage.md) | Run directories, resuming, caching, programmatic access |

## Going further

| Page | What's in it |
| --- | --- |
| [CI recipes](ci.md) | Gating, baselines, cost ceilings, report artifacts |
| [Evaluating judges](evaluating-judges.md) | Calibrating an autorater against human labels |
| [MCP server](mcp.md) | Driving evaling from an agent |
| [Claude Code plugin](plugin.md) | Installing evaling as a plugin: MCP server, command, and skill |
| [Python API](python-api.md) | Using evaling as a library |
| [No-look evals](no-look.md) | Evaluating data nobody is permitted to read |
| [Troubleshooting](troubleshooting.md) | Symptoms, causes, fixes |

## Contributing

- [Architecture](architecture.md) — how it's put together, and why
- [CONTRIBUTING.md](../CONTRIBUTING.md) — setup, expectations, adding a
  provider or scorer
- [REQUIREMENTS.md](../REQUIREMENTS.md) — the design goals and their status
