---
description: Scaffold an evaling project and author an eval.yaml for a stated task
argument-hint: [what you want to evaluate]
allowed-tools: [Read, Write, Edit, Glob, Grep, Bash]
---

Set up an evaling project in the current directory and write a working
`eval.yaml` for this task:

$ARGUMENTS

Follow the `running-evals` skill. Work in this order:

1. **Scaffold.** Run `evaling init` for the commented config, `.gitignore` and
   secrets example. Do not overwrite an existing `eval.yaml` — if one is
   present, read it and extend it instead.

2. **Establish what is being compared.** If the task above does not say, ask.
   An eval needs at least two variants to be worth running; a single prompt
   produces a number with nothing to compare it against.

3. **Write the config.** Consult `references/config-reference.md` for legal
   keys and scorer parameters rather than guessing — unknown keys are load
   errors. Cases go inline while there are few of them, in a file once there
   are many.

4. **Choose scorers deliberately.** Prefer a deterministic scorer where the
   task admits one: `exact` or `regex` for a label, `json-schema` for a
   structured response. Reach for `llm-judge` only where correctness is
   genuinely a matter of judgement, and say so — it costs a model call per
   cell.

5. **Validate, then run small.** `evaling validate` first: it renders every
   prompt and reports the request count without spending anything. Then run
   with `--sample` and `--max-cost` before a full run.

6. **Pin the baseline** once the first run looks right, so later changes are
   measured against it rather than against nothing.

Report the request count and estimated cost before the first billable run, and
let the user decide whether to proceed.
