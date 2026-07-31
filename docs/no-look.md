# No-look evals

Evaluating data that humans are not allowed to read.

The situation: your prompt needs validating against real production traffic,
and the reason it needs validating against *real* traffic is the same reason
nobody may look at it. You want the scores. You cannot have the data.

"No-look" is evaling's name for it. The established vocabulary calls this
**eyes-off** evaluation — you may run code against data you are not permitted
to view — and what it does to the results is **data minimization**: only the
statistics leave. If you are fitting this into a governance process, it is the
same shape as a trusted research environment, where analysts run code against
sensitive data and only vetted aggregates are released.

It is **not** differential privacy. evaling makes no formal guarantee about
what an aggregate reveals, and deliberately does not enforce a minimum group
size — see [how many cases do you need](large-datasets.md#how-many-cases-do-you-need)
for what
that leaves you responsible for. Avoid calling it "blind evaluation" too: in
ML that already means blinding the *rater* to which system produced an output,
which is a different technique for a different problem.

No-look mode keeps case data out of every artifact evaling produces. It is
usually paired with a [case source](large-datasets.md), since data you may not
read generally lives in a system rather than a file — but the two are separate
features and either works without the other. This page covers the privacy
half; [large-datasets.md](large-datasets.md) covers streaming cases in.

Worked example: [`examples/no-look/`](../examples/no-look/).

---

## Who this is for, and where it has to run

This exists for organizations whose production data sits behind real access
controls — regulated industries, data-residency obligations, contractual
processing terms, or an internal policy that engineers do not read customer
records. The prompt still has to be validated against that data, and the
person doing the validating still may not see it.

**evaling must run inside the boundary.** Your case source runs in evaling's own
process and hands back plaintext; the model call needs the real prompt; the
scorer needs the real output. The data is in memory throughout. No-look changes
what *persists and gets shared* — artifacts, reports, CI logs, the cache — and
nothing about who can reach the data. So it belongs on infrastructure already
cleared for that data. Running one from a laptop against a production API pulls
production data onto that laptop, and no setting undoes it. It complements
access control; it does not replace it.

What it buys you is the artifact problem. An ordinary eval writes prompts and
completions into `results.jsonl`, the cache, and an HTML report you attach to a
pull request — artifacts that get copied, reach people with no access to the
source system, and outlive the run. No-look produces only numbers, which is
what you wanted to share anyway.

**If you are allowed to read your eval data, don't use this.** It costs you the
cache (every run pays full price), direct inspection of failures,
`--log-requests`, and the MCP `render_prompt` tool — rendering a case is
reading it, so there is nothing that tool could usefully return.
Worthwhile only when the constraint is real.

---

## No-look mode

```yaml
privacy:
  no_look: true
```

or `evaling run --no-look`. The flag can turn it on; there is deliberately no
flag that turns it off.

### What is dropped

| Dropped | Why |
| --- | --- |
| Rendered prompts (`messages`) | They contain the case data |
| Model output | Usually contains it back |
| Scorer `detail` (except your own Python scorer's) | A scorer explains itself by quoting what it looked at |
| Criterion `error` | An exception message from a scorer holding the output |
| Attachments | Never written to `artifacts/` |
| Inline cases in the config snapshot | A config with inline cases *is* the data |
| Provider error bodies | A rejected request often comes back with the input attached |
| Case ids | See below |

### What survives

Variant, model, per-criterion scores and pass/fail, token counts, cost,
latency, whether a cell errored, and the shape of that error. All the
aggregates, gating, and comparison features work normally — the numbers are
the deliverable.

```json
{"case_id": "case-2be299046ace7bbe", "variant": "acknowledge", "model": "mock",
 "messages": [], "output": null, "input_tokens": 52, "latency_ms": 0.211,
 "scores": {"brief": {"score": 1.0, "passed": true,
            "detail": "within the 60-word limit"}}}
```

### Case ids are hashed by default

An id from a production system is often an email address, an order number, or
an account reference — it identifies a record as surely as the record does. So
ids become a stable hash (`case-2be299046ace7bbe`), which still lets you follow
a case across a matrix and compare it between runs.

If your ids are genuinely opaque and you need them for lookup:

```yaml
privacy:
  no_look: true
  keep_case_ids: true
```

To find a hashed case in your own data, hash yours the same way:
`"case-" + sha256(id).hexdigest()[:16]`, or `evaling.hash_case_id(id)`.

### Scorers are the redaction boundary

This is the part that makes no-look usable rather than merely safe.

You cannot debug a regression by reading failures — that is the point, and it
also removes your normal way of working. The escape valve is the scorer: it
sees the real output, and it decides what is safe to say about it.

```python
def score(output: str, case: dict):
    missing = [f for f in REQUIRED_FIELDS if f not in output]
    if missing:
        return {
            "score": 0.0,
            "passed": False,
            "detail": f"missing fields: {', '.join(missing)}",
        }  # safe
    return {"score": 1.0, "passed": True, "detail": "all fields present"}
```

**Only a Python scorer's `detail` survives**, because you wrote it and you are
the right person to decide what may leave. Every other scorer's `detail` is
dropped, because a scorer that evaluates case content tends to quote it when
explaining itself: a failing `contains` reports the `expected` value it was
looking for, `agreement` reports the label and the extracted verdict, and a
judge's rationale quotes the text it graded. A criterion's `error` is replaced
too — it is an exception message from a scorer that had the output in hand.

### Nothing is written and then deleted

Case data is never written to disk in the first place. Redaction happens the
moment a cell finishes scoring, before the record reaches storage — so there
is no window in which case content exists on disk, and no cleanup step whose
failure would matter, which is the step a killed process cannot perform.

evaling does use temporary files, for a different purpose: `run.json` and
artifacts are written temp-then-rename so a reader never sees a half-written
file. Under no-look those files carry no case content — the records are
already redacted, and `artifacts/` is never populated at all.

Three limits this does not cover, none of which evaling can close: the OS may
page memory to swap; the `command` provider hands case data to a subprocess
whose behaviour is its author's responsibility; and attachment source files
are already on your disk, since evaling reads them rather than copying them.

### Judge calls are governed like any other

A judge is a billable model call, so it passes through `--max-cost` and the
judge model's own `max_concurrency` and `requests_per_minute`. Its spend counts
against the ceiling.

### The response cache is disabled

The cache stores prompts and completions verbatim on disk — precisely what
no-look prevents. It is turned off for the whole run, so re-running costs full
price. Combine with `limit` and `--max-cost`.

### Resume works, unless the cases come from a source

No-look does not affect resume: a no-look run over inline or file-backed cases
resumes like any other, matching stored records by their hashed ids.

Source-backed runs cannot be resumed, for reasons that are about the source
rather than about privacy — see
[why resume is refused](large-datasets.md#why-resume-is-refused). Since no-look
is usually paired with a source, that is the combination you are most likely to
meet. Bound those runs with `limit` and `--max-cost` instead.

### LLM judges are allowed — and are your call

A judge sends case data to a second model provider. Whether that is acceptable
is a compliance question about *your* data and *that* vendor, and evaling
cannot answer it for you, so it does not block you. Judge rationales are
suppressed from artifacts, but the data still leaves your process to reach the
judge. If that matters, use a Python scorer, or a judge model you host.

---

## Verifying it yourself

Don't take the claim on trust — the test suite doesn't:

```sh
cd examples/no-look
evaling run
grep -r "@example.com" .evaling/     # nothing
```

`tests/test_no_look.py` does this systematically: seeds every case with a
unique canary, runs the whole tool over it — run, show, `--verbose`, all four
export formats, the HTML report — then searches every artifact and every
rendered surface for the canary. It also asserts the canary *is* found without
no-look, because a test that cannot fail proves nothing.

## See also

- [large-datasets.md](large-datasets.md) — case sources, bounding a run, and
  how many cases you need
- [scoring.md](scoring.md) — Python scorers, which are the redaction boundary
- [secrets.md](secrets.md) — where API keys come from
