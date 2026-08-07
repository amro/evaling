# Large datasets

Inline cases and dataset files both assume your cases are small enough to keep
in a file and yours to check in. Past a certain size neither holds: the cases
live in a warehouse, a ticket system, or an API, there are hundreds of
thousands of them, and nobody wants a copy in git.

A **case source** is Python you write that evaling calls for pages of cases.
Cases stream a page at a time, so a run's memory is bounded by concurrency
rather than by case count — a run over 500,000 cases costs no more up front
than a run over ten.

If the data is also something you are not permitted to read, that is a second,
separate feature layered on this one: [no-look mode](no-look.md).

---

## Writing a source

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

## The contract

| Method | Required | Purpose |
| --- | --- | --- |
| `fetch(cursor, limit) -> CasePage` | yes | One page, plus where to continue |
| `count() -> int \| None` | no | Total, for progress and cost estimates |
| `close() -> None` | no | Cleanup when the run ends, including on failure |

`fetch` may be `async def` — evaling awaits it if it is a coroutine. A
synchronous `fetch` runs in a thread, so a blocking HTTP client doesn't stall
the model calls already in flight. The same holds for `count` and `close`.

`Case` takes `id`, `vars`, `files`, `expected`, and `human_label`, exactly as
an inline case does, so a source can carry attachments like any other case.

You do not have to inherit from anything. `CaseSource` is a
[`Protocol`](https://peps.python.org/pep-0544/): any object with a `fetch`
method qualifies, so your class can live in your codebase and know nothing
about evaling. `BaseCaseSource` exists only for editor completion.

## Cursors

Cursor-based rather than offset-based, so that rows inserted or deleted during
a walk don't cause pages to skip or repeat. The cursor is opaque to evaling —
a row id, a timestamp, a page token, whatever your API uses. Return `None`
when there is nothing more.

A cursor that repeats is an error rather than an infinite loop.

**An empty page is not the end** unless its cursor is `None`. Filtering inside
your source empties a whole page whenever every row on it is filtered out, and
the walk continues past it. A source that returns a thousand empty pages in a
row while still promising more is treated as broken and raises.

A case without an `id` is numbered by position, like an inline or dataset case.
Uniqueness is not checked — that needs every id at once, which is what
streaming exists to avoid — so a source handing out duplicate ids produces
records that share one.

## Bounding a run

A source with no `limit` and no cost ceiling will not start:

```
this config fetches cases from a source with no `limit`, so the number of
model calls is whatever the source returns — and nothing here can interrupt
it. Set `limit` in the config, or pass --max-cost.
```

Only when nothing is watching: at a terminal this runs, because you can read
the progress and interrupt it. Unattended — CI, or an agent over MCP — there
is no Ctrl-C and the size is unknown even to whoever wrote the source, so it
is refused instead.

## Checking a source without a full run

```sh
evaling validate
```

Fetches the first page only and renders every prompt against it. That catches
the errors that matter — a template referring to a variable your rows don't
have, a missing attachment — without walking a production dataset to find
them. If your source implements `count()`, the reported request total is the
real one; otherwise the output says it is a sample.

## What sources cost you

Streaming cases removes the last part of a run whose memory grew with the
number of cases. Three things change in exchange:

- **`--case` filtering doesn't work.** evaling doesn't know the ids in advance.
  Filter inside your source (that is what `params` is for), or use `limit`.
- **Records aren't returned in memory.** The size is unknown up front, so
  `RunResult.records` is empty and `records_truncated` is set. Use
  `result.iter_records()`, which streams from disk at any size.
- **Runs can't be resumed.** See below.

## Why resume is refused

Deliberately refused, not merely unimplemented.

A source can return different rows on the second call — inserted, mutated,
aged out — or the population itself can move ("the last 24 hours" is a
different set at 09:00 and 15:00). A run whose halves describe different data
produces no error and plausible numbers, which is the worst way to be wrong. A
`stable: true` flag would be a promise evaling cannot verify.

Bound runs with `limit` and `--max-cost` instead. If a long run dies, start a
fresh one.

File- and inline-backed runs are unaffected: their data is fingerprinted, so
resume stays supported there.

## How many cases do you need?

Large-dataset runs raise the opposite question too: how few can you get away
with? evaling enforces no minimum — one case is a legitimate thing to evaluate
— but a pass rate from a small sample carries more uncertainty than people
expect.

Roughly, for a pass rate near 50% (the widest case), at 95% confidence:

| Cases | Margin of error |
| --- | --- |
| 30 | ±18 points |
| 100 | ±10 points |
| 400 | ±5 points |
| 1,000 | ±3 points |

So a prompt scoring 72% on 50 cases and one scoring 78% are not
distinguishable — the difference is inside the noise. If you are comparing two
variants on the same cases, what matters is the number of cases where they
*disagree*, not the total.

Going the other way, there is a point past which more cases buy precision you
cannot use. Getting from ±3 to ±1 point takes roughly ten times the cases and
ten times the spend, which is rarely the constraint worth relieving.

## Worked example

[`examples/no-look/`](../examples/no-look/) pages 100 cases out of a synthetic
source of 250. It also turns on no-look mode, but the source half stands alone
— delete the `privacy:` block and it is an ordinary large-dataset eval.

## See also

- [no-look.md](no-look.md) — for data you are not permitted to read
- [configuration.md](configuration.md) — the `cases:` block reference
- [python-api.md](python-api.md) — `CaseSource`, `CasePage`, `iter_records()`
- [storage.md](storage.md) — what a large run keeps in memory and on disk
