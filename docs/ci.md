# Using evaling in CI

`evaling run` is CI-native: its exit code is the verdict (`0` pass, `1` gate
failed, `--fail-fast` stopped the run, or `--max-cost` left it incomplete, `2`
config error), nothing ever prompts, and `--json`/`export` produce
machine-readable artifacts.

## Gate on absolute quality

```yaml
# eval.yaml
thresholds:
  min_pass_rate: 0.9
  min_score: 0.8
```

```yaml
# .github/workflows/evals.yml
- run: evaling run
  env:
    ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

## Gate on regressions against a baseline

Pin a blessed run once (e.g. after a release), then gate future runs against
it:

```sh
evaling run --label v1.4-release
evaling baseline set v1.4-release
```

```yaml
# eval.yaml
thresholds:
  baseline: regression   # fail if score or pass rate drops below the pinned run
```

The pin lives in the output directory (`.evaling/runs/baseline`), so CI needs
a persisted output dir (a cache step, an artifact, or a shared volume) — or
pass an explicit run with `evaling run --baseline <run-id>`.

## Useful patterns

- **Lint eval configs on every PR** (free, no model calls):

  ```yaml
  - run: evaling run --dry-run
  ```

- **Cost ceiling** so a bad matrix can't burn the budget:

  ```yaml
  - run: evaling run --max-cost 5.00
  ```

- **Smoke check before the real matrix**, so a broken prompt costs one cell
  instead of all of them:

  ```yaml
  - run: evaling run --sample 10 --fail-fast
  - run: evaling run --baseline regression
  ```

  `--fail-fast` stops at the first failing cell and exits 1 whether or not
  thresholds are configured. Cells already in flight still finish and are
  recorded, so the partial run is worth reading.

  A sampled run is still gated: `thresholds` are evaluated over the sample, so
  a small one can fail the build on noise. The same is true of a run cut short
  by `--max-cost` — thresholds see the cells that ran. If none did, there is
  no verdict at all and the run exits `1` as incomplete rather than claiming a
  regression it never measured. Use `--sample` for the smoke check
  and let the unsampled run be the one that decides.

- **Publish a report artifact** — a single self-contained HTML file, viewable
  straight from the Actions artifact download:

  ```yaml
  - run: evaling run --html eval-report.html
  - uses: actions/upload-artifact@v4
    if: always()          # publish the report even when the gate fails
    with: {name: eval-report, path: eval-report.html}
  ```

  For a PR comment, the markdown export pastes cleanly:

  ```yaml
  - run: evaling export latest --format md --out eval-report.md
  ```

- **Cache model responses between runs** — persist `.evaling/cache/` with your
  CI cache; unchanged cells then cost nothing:

  ```yaml
  - uses: actions/cache@v4
    with:
      path: .evaling/cache
      key: evaling-cache-${{ hashFiles('eval.yaml', 'prompts/**', 'cases.jsonl') }}
  ```

- **Script against results** with `--json`:

  ```sh
  evaling --json run | jq -e '.gate.passed'
  ```

  `gate` is `null` when there was no verdict to give — no thresholds
  configured, or no cell ran (see `--max-cost` below). `jq -e` fails on
  `null`. A run that evaluated nothing also exits `1` on its own, so an empty
  source or an exhausted budget cannot take a build green; a run with no
  thresholds configured exits `0`, which is the same as it ever did.

## Gating on production data

To gate against real traffic that CI's logs must not contain, combine a
[case source](no-look.md) with no-look mode:

```yaml
cases:
  source: sources/prod.py:make_source
  page_size: 200
  limit: 500

privacy:
  no_look: true

thresholds:
  min_pass_rate: 0.95
  baseline: regression
```

```yaml
- run: evaling run --max-cost 5.00 --html report.html
  env:
    ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
- uses: actions/upload-artifact@v4
  if: always()
  with: {name: eval-report, path: report.html}
```

The gate, the summary, and the uploaded report all work normally — they
contain scores, not case content — so the artifact is safe to keep and share.

Two differences from an ordinary CI eval. The response cache is disabled in
no-look mode, so every run pays full price; keep `limit` and `--max-cost`
tight. And source-backed runs can't be resumed, so a job that dies starts over
— which is another reason to bound it.
