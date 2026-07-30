# Evaluating a RAG pipeline

The system under test here is not a prompt and not a model — it's
`pipeline.py`: retrieve from a corpus, then answer. That is what the `command`
provider is for. evaling sends a request on stdin and reads an answer on
stdout; everything in between is yours, so an agent, a RAG chain, or a local
binary is as evaluable as a chat API.

```sh
evaling validate     # renders every prompt against every case; calls nothing
evaling run          # 2 prompts x 2 retrieval configs x 9 questions
evaling show latest --failures
```

Runs offline. Retrieval is real; generation is a deterministic stand-in, so
this example is about the plumbing rather than about a model. Swap
`synthesize()` for a real model call and nothing else changes — not the
config, not the scorers, not the report.

## What it measures

Two axes, both properties of the pipeline rather than of the wording:

**The prompt changes pipeline behaviour.** `pipeline.py` reads directives out
of the system message: "only from the documents" makes it decline when
retrieval finds nothing relevant, and "cite" makes it name its source. Two of
the nine questions are not covered by the corpus at all. The grounded prompt
says so; the plain one answers from whatever came back first, which is the
failure mode that matters and the one a spot check never catches.

**The retrieval configuration is a matrix dimension.** The two entries under
`models:` are one pipeline with different `top_k`. `params` is passed through
to the script, so anything it can read — chunk size, reranker, index version —
can go on the matrix. One question's answer lives in the second-ranked
document, so `top_k: 1` cannot reach it however good the prompt is.

That second point is the argument for evaluating the system: a prompt eval
would have told you the prompt was fine.

## Reading the result

```
Variant   Model         Score   Pass rate
grounded  retrieve-1    0.833       77.8%
grounded  retrieve-3    0.917       88.9%
plain     retrieve-1    0.417        0.0%
plain     retrieve-3    0.500        0.0%
```

`plain` scores 0.4–0.5 while passing nothing, which is the useful shape: it
gets several answers right and is never *trustworthy*, because it neither
cites a source nor admits when it has none. A pipeline that is right most of
the time and confabulates the rest is not "mostly good".

The overall pass rate spans a configuration built to be bad, so the gate is
set low on purpose. The matrix is the thing to read.

## The scorers

- `scorers/answered.py` — the expected fact for a question the corpus answers,
  and an admission for one it doesn't. Both are the same question for a
  retrieval system: does the answer reflect what was actually retrieved?
- `scorers/cited.py` — every answer names its source. Declining counts as
  cited, since there is nothing to name; scoring it a failure would make
  honesty rank below confabulation.

## Making it real

1. Point `CORPUS` at your documents, or replace `retrieve()` with your index.
2. Replace `synthesize()` with your model call.
3. Replace `cases.jsonl` with questions you actually get — including ones your
   corpus does not answer, which is the half people leave out.

See [providers.md](../../docs/providers.md) for the full `command` protocol.
