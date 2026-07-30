"""Scaffold a calibration eval: an eval whose subject is your judge.

docs/evaluating-judges.md describes a specific config shape — rubrics become
the variants, real model outputs paired with human ratings become the cases,
and the ``agreement`` scorer grades each verdict against the rating. That is
mechanical enough to generate, and the step between "I have a judge" and "I
trust this judge to gate CI" is the one people skip, so leaving it as prose
was leaving the barrier where it was.

This generates and does not run. It calls no model and costs nothing; the
output is a directory you read, edit, and then `evaling run`.
"""

import csv
import io
import json
from pathlib import Path
from typing import Any

from evaling.errors import EvalingError
from evaling.storage import ResultRecord
from evaling.textfile import read_text

#: What a rating column may be called, so the common spellings just work.
LABEL_COLUMNS = ("human_label", "label", "rating", "score")
#: Rubrics to scaffold. Two, because the point is comparing phrasings — one
#: rubric has nothing to be better than.
RUBRIC_NAMES = ("strict", "lenient")


class CalibrationError(EvalingError):
    """A calibration set could not be assembled."""


def load_labels(path: Path) -> dict[str, Any]:
    """Human ratings, keyed by case id. CSV or JSONL, whichever you have."""
    text = read_text(path, CalibrationError, missing=f"labels file not found: {path}")
    rows = _rows(path, text)
    labels: dict[str, Any] = {}
    for number, row in enumerate(rows, start=1):
        case_id = row.get("case_id") or row.get("id")
        if not case_id:
            raise CalibrationError(
                f"{path}:{number}: every row needs a `case_id` (or `id`) naming the case it rates"
            )
        label = next((row[name] for name in LABEL_COLUMNS if row.get(name) not in (None, "")), None)
        if label is None:
            raise CalibrationError(
                f"{path}:{number}: no rating found — name the column one of "
                f"{', '.join(LABEL_COLUMNS)}"
            )
        labels[str(case_id)] = _number_if_possible(label)
    if not labels:
        raise CalibrationError(f"{path} has no rows")
    return labels


def _rows(path: Path, text: str) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".csv":
        try:
            return list(csv.DictReader(io.StringIO(text, newline="")))
        except csv.Error as exc:
            raise CalibrationError(f"{path}: could not parse as CSV: {exc}") from exc
    rows = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CalibrationError(f"{path}:{number}: invalid JSON: {exc.msg}") from exc
        if not isinstance(row, dict):
            raise CalibrationError(f"{path}:{number}: each line must be a JSON object")
        rows.append(row)
    return rows


def _number_if_possible(value: Any) -> Any:
    """A rating read from CSV is a string; the agreement scorer wants a number."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    try:
        text = str(value).strip()
        return int(text) if text.lstrip("-").isdigit() else float(text)
    except ValueError:
        return value


def build_cases(
    records: list[ResultRecord],
    labels: dict[str, Any],
    *,
    variant: str | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """Pair each rated output with its rating: one row per rated case.

    A run with several variants or several models produced several different
    answers per case, and the rating refers to one of them. Picking whichever
    happened to be written first would build a calibration set measuring
    agreement against outputs the rater may never have seen — plausible
    numbers about the wrong thing — so it is an error to be ambiguous here.
    """
    usable = [r for r in records if r.error is None and r.output]
    for kind, chosen, present in (
        ("variant", variant, {r.variant for r in usable}),
        ("model", model, {r.model for r in usable}),
    ):
        # Checked before filtering: a name that matches nothing otherwise ends
        # up reported as "no case matches a labelled id", which sends the
        # reader to look at their ratings instead of their typo.
        if chosen is not None and chosen not in present:
            raise CalibrationError(
                f"the run has no {kind} {chosen!r} (it has: {', '.join(sorted(present))})"
            )
    if variant is not None:
        usable = [r for r in usable if r.variant == variant]
    if model is not None:
        usable = [r for r in usable if r.model == model]
    for kind, chosen, values in (
        ("variant", variant, {r.variant for r in usable}),
        ("model", model, {r.model for r in usable}),
    ):
        if chosen is None and len(values) > 1:
            raise CalibrationError(
                f"the run has several {kind}s ({', '.join(sorted(values))}), so it is "
                f"ambiguous which output your ratings refer to. Pass --{kind} to choose one."
            )

    cases, seen = [], set()
    for record in usable:
        if record.case_id in seen or record.case_id not in labels:
            continue
        seen.add(record.case_id)
        cases.append(
            {"id": record.case_id, "answer": record.output, "human_label": labels[record.case_id]}
        )
    # Sorted, because records come back in completion order — which varies
    # with concurrency. A generated file that differs between two identical
    # invocations is a file nobody can diff.
    cases.sort(key=lambda case: case["id"])
    if not cases:
        unmatched = sorted(labels)[:3]
        raise CalibrationError(
            "no case in the run matches a labelled id — the labels file names "
            f"{', '.join(unmatched)}{'…' if len(labels) > 3 else ''}, which this run does not "
            "contain. Check that the labels refer to this run's case ids."
        )
    return cases


def scaffold(
    out_dir: Path, cases: list[dict[str, Any]], *, judge_model: str, unlabelled: int = 0
) -> list[Path]:
    """Write the calibration project. Returns the files created."""
    if out_dir.exists() and any(out_dir.iterdir()):
        raise CalibrationError(f"{out_dir} already exists and is not empty")
    (out_dir / "rubrics").mkdir(parents=True)

    written = []
    calibration = out_dir / "calibration.jsonl"
    calibration.write_text(
        "".join(json.dumps(case, sort_keys=True) + "\n" for case in cases),
        encoding="utf-8",
        newline="\n",
    )
    written.append(calibration)

    for name in RUBRIC_NAMES:
        path = out_dir / "rubrics" / f"{name}.yaml"
        path.write_text(_rubric(name), encoding="utf-8", newline="\n")
        written.append(path)

    config = out_dir / "eval.yaml"
    config.write_text(_config(judge_model, len(cases), unlabelled), encoding="utf-8", newline="\n")
    written.append(config)

    readme = out_dir / "README.md"
    readme.write_text(_readme(len(cases)), encoding="utf-8", newline="\n")
    written.append(readme)
    return written


def _rubric(name: str) -> str:
    """Two phrasings that genuinely disagree, so the comparison has an answer."""
    if name == "strict":
        instruction = (
            "Rate the answer from 1 to 5. Be demanding: award 5 only for an answer "
            "that is both correct and complete, and deduct for anything hedged, "
            "partial, or padded."
        )
    else:
        instruction = (
            "Rate the answer from 1 to 5. Judge whether it would satisfy the person "
            "who asked: award 5 for an answer that is correct and useful, and do not "
            "deduct for phrasing, brevity, or hedging."
        )
    return (
        "# One rubric under test. Edit freely — comparing phrasings is the point.\n"
        "- role: system\n"
        f"  content: |\n    {instruction}\n"
        '    Reply with JSON only: {"score": <1-5>, "rationale": "<one sentence>"}\n'
        "- role: user\n"
        "  content: |\n    {{ answer }}\n"
    )


def _config(judge_model: str, labelled: int, unlabelled: int) -> str:
    note = ""
    if unlabelled:
        note = (
            f"# {unlabelled} case(s) in the run had no rating and were left out.\n"
            "# A calibration set is only as good as its labels.\n"
        )
    return f"""\
# Calibration eval: the thing under test is the judge prompt, not the model.
#
# Each variant is a rubric. The "model output" is the judge's verdict, and the
# agreement scorer grades that verdict against the human rating you supplied.
# The rubric with the best agreement is the one worth trusting.
#
#   evaling validate    # check it renders; calls nothing
#   evaling run         # {labelled} cases x {len(RUBRIC_NAMES)} rubrics
#
{note}
models:
  - id: {judge_model}
    provider: anthropic          # change to match the model you judge with
    params: {{max_tokens: 256}}

variants:
  - name: rubric-strict
    prompt: rubrics/strict.yaml
  - name: rubric-lenient
    prompt: rubrics/lenient.yaml

cases:
  file: calibration.jsonl

scorecard:
  # Did the judge land on exactly the human's rating?
  - criterion: exact-agreement
    scorer: {{type: agreement}}
  # ...and is it at least in the right neighbourhood? Usually the one that
  # matters: a judge one point off is useful, a judge three points off is not.
  - criterion: close-agreement
    scorer: {{type: agreement, mode: within, tolerance: 1}}

thresholds:
  # Demand this much close agreement before letting the judge gate anything.
  min_pass_rate: 0.8
"""


def _readme(labelled: int) -> str:
    return f"""\
# Judge calibration

Generated by `evaling calibrate`. Nothing here has run yet.

`calibration.jsonl` holds {labelled} answers from your run, each paired with
the rating you gave it. `rubrics/` holds two phrasings of the same judging
task, deliberately different, so the run has something to compare.

```sh
evaling validate     # renders every rubric against every case; calls nothing
evaling run          # then read the matrix
```

The matrix is agreement rates per rubric. `close-agreement` is usually the
number to read: a judge one point off a human is useful, a judge three points
off is not.

Before trusting the result:

- **Check the labels, not just the score.** A rubric that agrees with careless
  ratings has learned to be careless.
- **30–50 rated answers** is usually enough to separate rubric candidates.
  Fewer than that and the difference between two rubrics is inside the noise —
  see https://github.com/amro/evaling/blob/main/docs/large-datasets.md
- **Edit the rubrics.** The two here are a starting point, not candidates
  someone chose for your task.

Once a rubric wins, it becomes the `rubric:` of a judge in your real eval's
`judges:` block. See
https://github.com/amro/evaling/blob/main/docs/evaluating-judges.md
"""
