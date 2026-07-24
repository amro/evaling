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
| `totals` | `dict` | Token counts and `cost_usd` |
| `aggregates` | `dict` | `overall` plus per variant × model |
| `gate` | `GateResult \| None` | `None` when no thresholds are configured |
| `warnings` | `list[str]` | Non-fatal issues, e.g. loose secrets-file permissions |

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

`dry_run` renders every prompt and raises on the first template or config
problem, without issuing a request. This is what `evaling validate` calls.

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
prompt files, datasets, and attachments must be absolute.

## Errors

Every failure evaling raises deliberately descends from `EvalingError`:

| Exception | Raised when |
| --- | --- |
| `ConfigError` | The config or a dataset is invalid |
| `TemplateError` | A prompt failed to render |
| `ContentError` | An attachment is missing, unreadable, or unsupported |
| `ScoringError` | A scorer failed |
| `StorageError` | A run can't be read, written, or resumed |

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

Sweep a parameter and pick the best variant:

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
