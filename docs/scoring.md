# Scoring: scorers, scorecards, judges, and thresholds

Quality in evaling is a **scorecard**: named, weighted criteria, each backed by
a scorer. Every matrix cell (variant × model × case) gets per-criterion scores;
the run gets aggregates; thresholds turn aggregates into a CI pass/fail.

## Scorecard

```yaml
scorecard:
  - criterion: accuracy
    weight: 3
    scorer: {type: llm-judge, judge: quality-judge}
  - criterion: format
    weight: 1
    scorer: {type: json-schema, schema: schemas/answer.json}
```

Every scorer produces a **score in [0, 1]**, an explicit **pass/fail**, and
optional detail. Per cell: the weighted mean of criterion scores; the cell
passes only if *every* criterion passed. Cells whose model call failed score 0
and fail — errors count against you rather than vanishing from the stats.

A scorer that itself crashes fails its criterion (the error is recorded in the
cell's scores) but never aborts the run.

## Built-in scorers

Keys other than `type` are parameters.

| Type | Checks | Parameters |
|---|---|---|
| `exact` | output equals the expected value | `value` (else case `expected`), `case_sensitive` (true), `strip` (true) |
| `contains` | output contains the needle | `value` (else case `expected`), `case_sensitive` (true) |
| `not-contains` | output does not contain it | same as `contains` |
| `regex` | pattern found in output (`re.search`) | `pattern` (required), `case_sensitive` (true) |
| `json-valid` | output parses as JSON | — (markdown fences tolerated) |
| `json-schema` | JSON matches a schema | `schema`: inline mapping or file path |
| `llm-judge` | an autorater grades the output | `judge` (required), `scale` (1), `pass_at` (0.5) |
| `python` | your function grades the output | `file` (required), `function` ("score"), `pass_at` (1.0) |
| `agreement` | verdict matches `human_label` | `mode` (`exact`/`within`), `tolerance` (0), `field` ("score") |

## LLM judges

A judge is a first-class prompt: a text-only rubric plus a judge model, defined
once and reusable across criteria. Image and audio parts are rejected both in
inline rubrics and in referenced rubric files. Resume fingerprints include the
rubric file itself; there is no supported rubric-media dependency to hash.

For example:

```yaml
models:
  - id: claude-sonnet-5
    provider: anthropic
    role: judge                   # required: grades, is not evaluated

judges:
  quality-judge:
    model: claude-sonnet-5        # an id from models
    rubric: prompts/judge-rubric.yaml
```

The judge's model must declare `role: judge` (or `role: both` if you also want
it evaluated as a candidate) — see
[role](configuration.md#role--what-a-model-is-here-for). Without it the config
is rejected, because a model that only judges should not silently become a
system under test.

A judge call is billed like any other, so it counts against `--max-cost` and
obeys the judge model's `max_concurrency` and `requests_per_minute`. Budget for
it: a scorecard with one judge roughly doubles the calls a run makes.

It is also **cached** like any other call, so re-running an unchanged eval
does not pay to reach the same verdict twice. Editing the rubric changes the
request and misses the cache, which is what you want; `--no-cache` forces a
fresh judgment when you want one anyway.

> **A judge sends your case data to another model.** That is a second
> processor with its own terms and its own retention. Whether that is
> acceptable for your data is your call, not evaling's, so it is permitted and
> not blocked — but decide it deliberately, especially when the cases are
> production data. In [no-look mode](no-look.md) judge rationales are dropped
> from all artifacts, but the data still leaves your process to reach the
> judge.

Rubric templates receive `output` (the text being graded), `expected`, and
`vars` (the case's variables):

```yaml
# prompts/judge-rubric.yaml
- role: system
  content: >
    Grade the answer 1-5 for accuracy. Respond with JSON:
    {"score": <1-5>, "rationale": "<one sentence>"}
- role: user
  content: |
    Question: {{ vars.question }}
    Expected: {{ expected }}
    Answer: {{ output }}
```

The judge must answer with JSON containing a numeric `score` (markdown fences
are tolerated). With `scale: 5`, a score of 4 normalizes to 0.8. The judge may
state `passed` explicitly; otherwise the cell passes when the normalized score
reaches `pass_at` (default 0.5). The `rationale` is stored as the criterion's
detail and shows up in reports.

## Python scorers

```yaml
scorer: {type: python, file: scorers/grade.py}
```

```python
# scorers/grade.py — sync or async
def score(output: str, case: dict):
    # return a bool, a number in [0, 1], or:
    return {"score": 0.9, "passed": True, "detail": "matched 9/10 facts"}
```

`case` is the full case as a dict (`vars`, `expected`, `human_label`, `files`).
A synchronous function runs in a thread, so a slow scorer doesn't stall the
model calls already in flight.

`pass_at` decides the verdict whenever the function does not: a bare number
passes at that value or above, and so does a mapping that omits `passed`. It
defaults to 1.0, and is checked when the scorer is built — before any cell runs
— so a wrong one is a config error rather than every cell scoring zero.

The file is loaded on its own, not imported into a package. Two consequences
worth knowing:

- **It cannot import its neighbours.** `import helper` for a `helper.py` beside
  it fails. Keep a scorer self-contained, or add its directory to `sys.path`
  inside the file.
- **Its classes cannot be pickled**, so a scorer cannot hand its own types to
  `multiprocessing`. Returning plain values is unaffected.

Loading it this way is deliberate: each run re-executes the file, so an edit
takes effect without a stale module cached from a previous run, and two
scorers with the same filename in different directories stay separate.

`sys.exit()` fails that criterion rather than ending the run — a script adapted
into a scorer often still calls it.

## Aggregates and thresholds

Every finished run stores aggregates in `run.json`: overall and per
variant × model group — `cases`, mean `score`, `pass_rate`, `errors`.

Thresholds gate the run (non-zero exit in CI):

```yaml
thresholds:
  min_pass_rate: 0.9     # overall pass rate must reach this
  min_score: 0.8         # overall weighted score must reach this
  baseline: regression   # and must not be worse than the pinned baseline
```

Both absolute checks and the baseline check can apply together; every check's
outcome (with detail) is stored in `run.json` under `gate`. `gate` is `null`
when there was no verdict to give — no thresholds configured, or no cell ran
(a run that evaluated nothing still exits non-zero; see [ci.md](ci.md)). `baseline` accepts
`regression` (compare against the pinned baseline run) or a specific run id.
A run is "worse than baseline" if either its overall score or pass rate drops
below the baseline's.
