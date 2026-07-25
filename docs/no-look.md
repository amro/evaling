# No-look evals

Evaluating data that humans are not allowed to read.

The situation: your prompt needs validating against real production traffic,
and the reason it needs validating against *real* traffic is the same reason
nobody may look at it. You want the scores. You cannot have the data.

Two pieces make this work — a **case source** that streams cases from your
systems instead of a file, and **no-look mode**, which keeps the data out of
every artifact evaling produces.

Worked example: [`examples/no-look/`](../examples/no-look/).

---

## Case sources

A source is an object you write. evaling calls it for pages of cases.

```python
# sources/prod_tickets.py
from evaling import BaseCaseSource, Case, CasePage


class ProdTickets(BaseCaseSource):
    def fetch(self, cursor, limit):
        page = my_api.query(after=cursor, limit=limit)
        return CasePage(
            cases=[Case(id=row["id"], vars={"ticket": row["body"]}) for row in page.rows],
            cursor=page.next_cursor,  # None means this was the last page
        )

    def count(self):  # optional
        return my_api.total()

    def close(self):  # optional
        my_api.disconnect()


def make_source():
    return ProdTickets()
```

Point the config at the factory:

```yaml
cases:
  source: sources/prod_tickets.py:make_source
  params: {region: eu}      # passed to make_source(**params)
  page_size: 200            # cases per fetch
  limit: 5000               # stop after this many
```

### The contract

| Method | Required | Purpose |
| --- | --- | --- |
| `fetch(cursor, limit) -> CasePage` | yes | One page, plus where to continue |
| `count() -> int \| None` | no | Total, for progress and cost estimates |
| `close() -> None` | no | Cleanup when the run ends, including on failure |

`fetch` may be `async def` — evaling awaits it if it is a coroutine. `Case`
takes `id`, `vars`, `files`, `expected`, and `human_label`, exactly as an
inline case does.

You do not have to inherit from anything. `CaseSource` is a
[`Protocol`](https://peps.python.org/pep-0544/): any object with a `fetch`
method qualifies, so your class can live in your codebase and know nothing
about evaling. `BaseCaseSource` exists only for editor completion.

### Cursors

Cursor-based, not offset-based, because that is what real APIs give you and
because offsets drift when the underlying table is still being written to. The
cursor is opaque to evaling — a row id, a timestamp, a page token, whatever
your API uses. Return `None` when there is nothing more.

A cursor that repeats is an error rather than an infinite loop.

### What sources cost you

Streaming cases removes the last part of a run whose memory grew with the
number of cases, so a source-backed run is bounded by `concurrency` regardless
of size. Two things change in exchange:

- **`--case` filtering doesn't work.** evaling doesn't know the ids in advance.
  Filter inside your source, or use `limit`.
- **Records aren't returned in memory.** The size is unknown up front, so
  `RunResult.records` is empty and `records_truncated` is set. Use
  `result.iter_records()`.

### Bounding a run

A source with no `limit` and no cost ceiling will not start:

```
this config fetches cases from a source with no `limit`, so evaling cannot
tell how many model calls the run will make. Set `limit` in the config, pass
--max-cost, or pass --yes to run it anyway.
```

An unbounded source pointed at a production table is a bill, not a run.

### Checking a source without a full run

```sh
evaling validate
```

Fetches the first page only and renders every prompt against it. That catches
the errors that matter — a template referring to a variable your rows don't
have, a missing attachment — without walking a production dataset to find
them. If your source implements `count()`, the reported request total is the
real one; otherwise the output says it is a sample.

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
| Judge rationales | A judge quotes the text it graded |
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
 "scores": {"brief": {"score": 1.0, "passed": true, "detail": "14 words"}}}
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

`detail` from a Python scorer survives no-look mode, because you wrote it and
you are the right person to decide what may leave. Details from an **LLM
judge** do not survive: a rationale quotes the text it graded, and no judge can
be relied on to keep that separation.

### The response cache is disabled

The cache stores prompts and completions verbatim on disk — precisely what
no-look prevents. It is turned off for the whole run, so re-running costs full
price. Combine with `limit` and `--max-cost`.

### Resume is not available

Deliberately refused, not merely unimplemented.

A source can return different rows on the second call — inserted, mutated,
aged out — or the population itself can move ("the last 24 hours" is a
different set at 09:00 and 14:00). A run whose halves describe different data
produces no error and plausible numbers, which is the worst way to be wrong. A
`stable: true` flag would be a promise evaling cannot verify.

Bound runs with `limit` and `--max-cost` instead. If a long run dies, start a
fresh one.

### LLM judges are allowed — and are your call

A judge sends case data to a second model provider. Whether that is acceptable
is a compliance question about *your* data and *that* vendor, and evaling
cannot answer it for you, so it does not block you. Judge rationales are
suppressed from artifacts, but the data still leaves your process to reach the
judge. If that matters, use a Python scorer, or a judge model you host.

---

## How many cases do you need?

No-look runs tend to be large, which raises the opposite question: how few can
you get away with? evaling does not enforce a minimum — one case is a
legitimate thing to evaluate — but a pass rate from a small sample carries more
uncertainty than people expect.

Roughly, for a pass rate near 50% (the widest case), at 95% confidence:

| Cases | Margin of error |
| --- | --- |
| 30 | ±18 points |
| 100 | ±10 points |
| 400 | ±5 points |
| 1,000 | ±3 points |

So a prompt scoring 72% on 50 cases and one scoring 78% are not distinguishable
— the difference is inside the noise. If you are comparing two variants on the
same cases, what matters is the number of cases where they *disagree*, not the
total.

Small groups also raise a re-identification question: "the one case from region
X failed" can identify a record even with no payload stored. evaling stays out
of that judgment; it depends on your data and your obligations.

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
