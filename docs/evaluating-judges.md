# Evaluating your judges (meta-evals)

An LLM judge is just a prompt plus a model — which means evaling can evaluate
judges with the same machinery it uses for everything else. This recipe answers
the question: *which rubric phrasing makes my judge agree with human judgment?*

## Generating it

`evaling calibrate` writes the whole thing from a run you have already made
plus a file of your ratings:

```sh
evaling run --label to-rate                  # produce answers to rate
# ...rate them: a CSV of case_id,human_label
evaling calibrate --from-run to-rate --labels ratings.csv
cd calibration && evaling validate && evaling run
```

That gives you the config below, filled in — your rated answers as cases and
two rubric phrasings to compare. It calls nothing and spends nothing; edit the
rubrics before running it, since the generated pair is a starting point rather
than candidates chosen for your task.

The rest of this page is what it generates and why, for when you want to build
it yourself or change its shape.

## Ingredients

1. A **calibration set**: real model outputs paired with human ratings. Store
   the output as a var and the human rating as `human_label`:

   ```jsonl
   {"id": "s1", "answer": "The Treaty of Ghent ended the war...", "human_label": 5}
   {"id": "s2", "answer": "I think it was probably France?", "human_label": 1}
   {"id": "s3", "answer": "It ended in 1815 (close, but off by a year).", "human_label": 3}
   ```

2. **Judge-prompt variants**: the rubrics you want to compare, as ordinary
   prompt variants. The "model output" your eval produces *is the judge's
   verdict*.

3. The **agreement scorer** to grade each verdict against the human label.

## The eval

```yaml
# judge-eval.yaml — the prompt under test IS the judge prompt
models:
  - id: judge-model
    provider: anthropic          # the model that will run your judge

variants:
  - name: rubric-strict
    prompt: rubrics/strict.yaml
  - name: rubric-detailed
    prompt: rubrics/detailed.yaml

cases:
  file: calibration.jsonl

scorecard:
  - criterion: exact-agreement
    scorer: {type: agreement}                    # verdict score == human_label
  - criterion: close-agreement
    scorer: {type: agreement, mode: within, tolerance: 1}

thresholds:
  min_pass_rate: 0.8   # demand 80% close agreement before trusting the judge
```

Each rubric variant asks the judge to rate `{{ answer }}` and answer with JSON
(`{"score": <1-5>, "rationale": "..."}`). The agreement scorer parses the
verdict's `score` field and compares it to `human_label` — `exact` for strict
match, `within` + `tolerance` for near-misses.

Run it and read the summary like any eval: the variant × model matrix now
shows **agreement rates per rubric**. The winning rubric becomes the judge you
reference from your real evals' `judges:` block.

## Notes

- **Iterating is cheap**: with caching on, re-scoring after a scorer tweak
  costs nothing; only new rubric variants pay for model calls.
- **Correlation and kappa**: per-cell agreement (exact / within-N) is built in.
  Rank correlation or Cohen's kappa across the whole calibration set can be
  computed from an export (`evaling export <run> --format json`) with a few
  lines of pandas/scipy — a built-in aggregate may come later.
- **Keep the calibration set honest**: sample real outputs (good, bad, and
  weird), not synthetic ones; 30-50 labeled examples is usually enough to
  separate rubric candidates.
