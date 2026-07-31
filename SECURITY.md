# Security policy

## Reporting a vulnerability

Please don't open a public issue.

Use GitHub's **private vulnerability reporting**: the *Report a vulnerability*
button on this repository's [Security tab](../../security). It reaches the
maintainer directly and stays private until an advisory is published.

If you can't use it, open an issue saying you have a security report and asking
how to send it — without the details.

Expect an acknowledgement within a week. This is a small project maintained by
one person, so please allow reasonable time for a fix before disclosing.

## Supported versions

Pre-1.0: fixes land on the latest release only. There are no maintained
older branches.

## What counts as a vulnerability here

evaling runs your prompts against model APIs, reads your config and data
files, and stores results locally. The things worth reporting:

- **A path where an API key can leak.** Keys are never read from config files,
  never written into `os.environ`, and are redacted from errors, logs, and
  stored results. A way around any of that is a security report, not a bug
  report.
- **A no-look leak.** In [no-look mode](docs/no-look.md), case content must not
  reach any artifact — results, exports, reports, the cache, or the MCP
  responses. A path that surfaces prompts, outputs, or attachments is a
  vulnerability, because the mode exists for data nobody is permitted to read.
- **Escaping the config's directory.** A relative attachment path in a dataset
  may not resolve outside the file that declared it; datasets arrive from
  elsewhere, and evaling reads, transmits, and archives whatever they point at.
- **Code execution from data.** Prompt templates and case content are rendered,
  not executed. A case that gets code to run is a vulnerability.

## What doesn't

- **`python` scorers, `command` providers, and case sources run your own code
  by design.** They are configuration pointing at scripts you wrote; that is
  the feature, not a sandbox escape.
- **Sending case data to a model provider**, including an LLM judge. That is
  what evaluation is. Whether a given provider may see your data is a decision
  evaling deliberately leaves to you.
- **Anything requiring write access to `eval.yaml` or the secrets file.** Both
  are trusted inputs; someone who can edit them can already run code as you.
