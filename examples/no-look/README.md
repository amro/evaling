# No-look eval

Evaluating production data that nobody is allowed to read afterwards.

The cases come from a **source** — Python you write that pages an API — rather
than a file, and `privacy.no_look` keeps prompts, outputs, judge rationales,
and attachments out of every artifact. Scores, counts, and timings survive.

The two halves are independent. Delete the `privacy:` block and this is an
ordinary large-dataset eval; see [large-datasets.md](../../docs/large-datasets.md).

```sh
cd examples/no-look
evaling run
evaling show latest
```

Then check the claim yourself:

```sh
grep -r "@example.com" .evaling/    # nothing
grep -r "ORD-" .evaling/            # nothing
cat .evaling/runs/*/results.jsonl   # scores, no content
```

```json
{"case_id": "case-2be299046ace7bbe", "messages": [], "output": null,
 "scores": {"brief": {"detail": "within the 60-word limit",
                      "passed": true, "score": 1.0}},
 "variant": "acknowledge", "model": "mock", "input_tokens": 52}
```

`sources/prod_tickets.py` generates synthetic tickets carrying names, emails,
and order numbers — the material that normally makes an eval run impossible to
share. A real source is the same shape: page your API, hand back `Case`
objects.

This example is safe to run anywhere because its data is invented. A real one
is not: your source hands plaintext to evaling's own process, so a no-look run
belongs on infrastructure already cleared for that data. No-look controls what
the run *leaves behind*, not who can reach the data — see
[who this is for](../../docs/no-look.md#who-this-is-for-and-where-it-has-to-run).

## What it demonstrates

**Case ids are data too.** These ids are email addresses. They are hashed by
default (`case-2be299...`), stably, so a case can still be followed across a
matrix and between runs. Set `privacy.keep_case_ids: true` if your ids are
genuinely opaque.

**Scorers are the redaction boundary.** `scorers/brief.py` sees the real
output and emits only a verdict and a safe detail (`"within the 60-word
limit"`). That detail
is the only thing about the output that reaches a report — so what you put
there is the decision about what leaves the boundary.

**Bounded by construction.** `limit: 100` caps the run. Without a `limit`,
evaling asks for `--max-cost` or `--yes` first, since the call count would
otherwise be whatever the source returns.

**No resume.** Deliberate. A live source can return different rows on the
second call — inserted, mutated, aged out — and evaling cannot verify that it
didn't. Half a run under one population and half under another looks entirely
normal and is wrong. See [no-look.md](../../docs/no-look.md).

## Adapting it

Replace `sources/prod_tickets.py` with your own:

```python
from evaling import BaseCaseSource, Case, CasePage


class MyTickets(BaseCaseSource):
    def fetch(self, cursor, limit):
        page = my_api.query(after=cursor, limit=limit)
        return CasePage(
            cases=[Case(id=r["id"], vars={"ticket": r["body"]}) for r in page.rows],
            cursor=page.next_cursor,  # None on the last page
        )

    def count(self):  # optional; enables a progress total
        return my_api.total()


def make_source():
    return MyTickets()
```

`fetch` may be `async def`. Full reference: [no-look.md](../../docs/no-look.md).
