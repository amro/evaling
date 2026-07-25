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
once and reusable across criteria:

```yaml
judges:
  quality-judge:
    model: claude-sonnet-5        # an id from models
    rubric: prompts/judge-rubric.yaml
```

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
outcome (with detail) is stored in `run.json` under `gate`. `baseline` accepts
`regression` (compare against the pinned baseline run) or a specific run id.
A run is "worse than baseline" if either its overall score or pass rate drops
below the baseline's.
