# Python API

Everything the CLI does is available programmatically. `evaling`'s top-level
module is the public surface — anything importable from it is supported;
anything deeper is internal and may change.

```python
from evaling import load_config, run_eval
```

## Running an eval

```python
from evaling import Settings, load_config, run_eval

config = load_config("eval.yaml")
result = run_eval(config, Settings(output_dir="out/runs", concurrency=4))

print(result.counts)  # {'total': 6, 'succeeded': 6, 'failed': 0, 'cached': 0}
print(result.totals)  # {'input_tokens': 103, 'output_tokens': 58, 'cost_usd': 0.0}
print(result.aggregates)  # 'overall' plus one entry per variant x model
print(result.gate)  # GateResult(passed=True, checks=[...]), or None
```

### `run_eval(config, settings=None, **kwargs) -> RunResult`

| Argument | Meaning |
| --- | --- |
| `label` | Human-friendly name for the run |
| `resume_run_id` | Finish an interrupted run instead of starting one |
| `baseline_run_id` | Gate against this run |
| `model_filter`, `variant_filter`, `case_filter` | Restrict the matrix |
| `max_cost_usd` | Stop issuing calls once spend reaches this |
| `on_result` | Callback per completed cell, for progress |

`on_result` is called from the event loop as each cell lands, before the run
finishes — that's how the CLI drives its progress bar:

```python
result = run_eval(config, on_result=lambda record: print(record.case_id, record.error or "ok"))
```

### Inside an existing event loop

`run_eval` creates a loop, so it cannot be called from within one. In async
code use `run_eval_async`, which takes the same arguments:

```python
from evaling import run_eval_async

result = await run_eval_async(config)
```

### `RunResult`

| Field | Type | Notes |
| --- | --- | --- |
| `run_id` | `str` | Also the directory name |
| `path` | `Path` | The run directory |
| `records` | `list[ResultRecord]` | One per cell |
| `counts` | `dict` | `total`, `succeeded`, `failed`, `cached` |
| `totals` | `dict` | Token counts, `cost_usd` (cells + judges), `judge_cost_usd` |
| `aggregates` | `dict` | `overall` plus per variant × model |
| `gate` | `GateResult \| None` | `None` when no thresholds are configured |
| `warnings` | `list[str]` | Non-fatal issues, e.g. loose secrets-file permissions |
| `records_truncated` | `bool` | True when the run was too large to keep in memory |

Each aggregate entry holds `cases`, `score`, `pass_rate`, and `errors`:

```python
result.aggregates["overall"]  # {'cases': 6, 'score': 1.0, 'pass_rate': 1.0, 'errors': 0}
```

`GateResult` has `passed` and `checks`, one entry per threshold that applied:

```python
result.gate.passed  # True
result.gate.checks  # [{'name': 'min_pass_rate', 'passed': True, 'detail': '...'}]
```

Exit non-zero in your own script the way the CLI does:

```python
import sys

if result.gate is not None and not result.gate.passed:
    sys.exit(1)
```

### Large runs

Above 10,000 cells (`evaling.engine.MAX_RETAINED_RECORDS`) a run stops handing
back every record: `records` is empty and `records_truncated` is True. Counts,
totals, and aggregates are unaffected — they're accumulated as the run
proceeds, not computed from a retained list.

`records` is empty rather than partial on purpose. A partial list looks usable
and would silently give you wrong answers; an empty one sends you here:

```python
for record in result.iter_records():  # streams from disk, any run size
    if record.error:
        print(record.case_id, record.error)
```

`iter_records()` works for every run, so code that uses it needs no size check:

```python
failures = [r for r in result.iter_records() if r.error]
```

### `ResultRecord`

One cell: `variant`, `model`, `case_id`, `messages` (what was sent), `output`,
`input_tokens`, `output_tokens`, `cost_usd`, `latency_ms`, `cached`, `error`,
and `scores`, keyed by criterion:

```python
record.scores  # {'accuracy': {'weight': 1.0, 'score': 1.0, 'passed': True}}
```

A criterion that produced an explanation (an LLM judge, or a Python scorer
returning one) also carries `detail`.

`error` and `output` are mutually exclusive in practice — a failed cell has
an error and no output. A cell that errored is not scored.

## Checking without calling anything

```python
from evaling import dry_run

report = dry_run(config, model_filter=["gpt-5.2"])
print(report.requests)  # how many calls a real run would make
print(report.cells)  # the matrix that would run
```

`dry_run` renders every prompt without issuing a request, and is what
`evaling validate` calls. A config problem — an unusable scorer, a sample
against a source — raises. A template problem is collected per cell in
`report.errors` instead, so one bad case does not hide the rest.

To see the matrix itself:

```python
from evaling import select_matrix

variants, models, cases = select_matrix(config, variants=["concise"])
```

## Reading stored runs

```python
from evaling import RunStore

store = RunStore("out/runs")

for meta in store.list_runs():
    print(meta["id"], meta["status"], meta["label"])

run_id = store.resolve_ref("latest")  # also accepts a label or 'baseline'
meta = store.load_meta(run_id)
records = store.load_results(run_id)
```

For a large run, stream instead of materializing every record:

```python
for record in store.iter_results(run_id):
    if record.error:
        print(record.case_id, record.error)
```

Baselines:

```python
store.set_baseline(run_id)
store.get_baseline()
```

## Scoring and comparison

```python
from evaling import aggregate, cell_summary, compare_aggregates

totals = aggregate(records)  # same shape as RunResult.aggregates
score, passed = cell_summary(records[0])  # weighted score for one cell
delta = compare_aggregates(old_meta["aggregates"], new_meta["aggregates"])
```

## Exporting

```python
from evaling import export_run

print(export_run(meta, records, "md"))  # json, csv, md, html
```

`html` produces the same self-contained single-file report as
`evaling export --format html`.

## Configuration

```python
from evaling import load_config, load_cases, resolve_settings

config = load_config("eval.yaml")  # validates, resolves paths, loads prompts
cases = load_cases(config)  # inline or dataset, paths resolved
settings = resolve_settings(cli={"concurrency": 4}, eval_settings=config.settings)
```

`resolve_settings` applies the same layering the CLI uses: CLI arguments >
`EVALING_*` environment variables > the config's `settings:` block > the user
config file > defaults.

Building a config in memory works too — useful for generated evals:

```python
from evaling import EvalConfig

config = EvalConfig.model_validate(
    {
        "models": [{"id": "mock", "provider": "mock"}],
        "variants": [{"name": "v1", "prompt": [{"role": "user", "content": "{{ q }}"}]}],
        "cases": [{"id": "c1", "vars": {"q": "hello"}, "expected": "hello"}],
        "scorecard": [{"criterion": "acc", "scorer": {"type": "contains"}}],
    }
)
```

A config built this way has no file to resolve relative paths against, so
prompt files, datasets, and attachments should be absolute.

## Case sources

Cases can come from your own code instead of a file. Implement `fetch`; the
rest is optional.

```python
from evaling import BaseCaseSource, Case, CasePage


class ProdTickets(BaseCaseSource):
    def fetch(self, cursor: str | None, limit: int) -> CasePage:
        page = my_api.query(after=cursor, limit=limit)
        return CasePage(
            cases=[Case(id=r["id"], vars={"ticket": r["body"]}) for r in page.rows],
            cursor=page.next_cursor,  # None on the last page
        )

    def count(self) -> int | None:  # optional
        return my_api.total()

    def close(self) -> None:  # optional
        my_api.disconnect()
```

`fetch` may be `async def`. Inheriting from `BaseCaseSource` is optional —
`CaseSource` is a `runtime_checkable` Protocol, so any object with a `fetch`
method qualifies:

```python
from evaling import CaseSource

isinstance(ProdTickets(), CaseSource)  # True
```

A source is referenced from the config by import path, since the config is
YAML and cannot hold an object:

```yaml
cases:
  source: sources/prod.py:make_source
  params: {region: eu}
  page_size: 200
  limit: 5000
```

Source-backed runs never return records in memory (the size isn't known up
front), so `records_truncated` is always True — use `iter_records()`.
`SourceError` is raised for a source that can't be loaded or that returns
something unusable. Full reference: [large-datasets.md](large-datasets.md).

## Privacy

```python
from evaling import hash_case_id, redact_record
```

`redact_record(record, keep_detail, hash_case_ids=True)` strips everything
derived from case content, in place. The engine calls it for you when
`privacy.no_look` is set; it's exported for the rare case of building your own
pipeline around `run_eval`.

`keep_detail` is the set of criterion names whose `detail` may survive — the
ones scored by your own Python function. Everything not named there is
dropped, so the safe default is `frozenset()`. Note the direction: this is a
whitelist of what to keep, not a list of what to remove.

`hash_case_id(case_id)` is the same stable hash no-look mode applies, so you
can find a hashed case in your own data:

```python
hash_case_id("order-8837")  # 'case-34fafabf4820edbf'
```

See [no-look.md](no-look.md).

## Errors

Every failure evaling raises deliberately descends from `EvalingError`:

| Exception | Raised when |
| --- | --- |
| `ConfigError` | The config or a dataset is invalid |
| `TemplateError` | A prompt failed to render |
| `ContentError` | An attachment is missing, unreadable, or unsupported |
| `ScoringError` | A scorer failed |
| `StorageError` | A run can't be read, written, or resumed |
| `SourceError` | A case source failed to load or returned something unusable |

```python
from evaling import EvalingError, load_config, run_eval

try:
    run_eval(load_config("eval.yaml"))
except EvalingError as exc:
    print(f"eval failed: {exc}")
```

Provider and scorer failures inside a *cell* are recorded on that record's
`error` field rather than raised — one bad cell shouldn't end a run you paid
for. Only whole-run problems raise.

## Config fingerprints

```python
from evaling import config_fingerprint

config_fingerprint(config)  # sha256 over the config and every file it references
```

Resume compares this. Two configs with the same fingerprint produce
comparable runs; a different fingerprint means results can't be mixed.

## A worked example

Sweep a parameter and pick the best value. (Substitute a parameter your model
actually accepts — evaling forwards `params` verbatim, so the vendor decides
what is valid, and `temperature` is not accepted by every current model.)

```python
from evaling import EvalConfig, load_config, run_eval

base = load_config("eval.yaml").model_dump()
best = None

for temperature in (0.0, 0.5, 1.0):
    config = EvalConfig.model_validate(
        {**base, "models": [{**base["models"][0], "params": {"temperature": temperature}}]}
    )
    result = run_eval(config, label=f"temp-{temperature}", max_cost_usd=1.00)
    score = result.aggregates["overall"]["score"]
    print(f"temperature={temperature}: {score:.3f}")
    if best is None or score > best[1]:
        best = (temperature, score)

print(f"best: {best[0]}")
```
