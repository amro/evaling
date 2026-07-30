"""Scorecard aggregation and threshold gating.

Per cell: the weighted mean of criterion scores; a cell passes only if every
criterion passed. Cells that errored (no output) score 0 and fail — they count
against pass rates rather than disappearing from them.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from evaling.config.schema import Thresholds
from evaling.storage import ResultRecord


def cell_summary(record: ResultRecord) -> tuple[float, bool]:
    """(weighted score, passed) for one record."""
    if record.error is not None or not record.scores:
        return 0.0, False
    total_weight = sum(entry.get("weight", 1.0) for entry in record.scores.values())
    if total_weight == 0:
        return 0.0, False
    score = (
        sum(entry.get("score", 0.0) * entry.get("weight", 1.0) for entry in record.scores.values())
        / total_weight
    )
    passed = all(entry.get("passed") is True for entry in record.scores.values())
    return round(score, 6), passed


@dataclass
class _RunningStats:
    """Sums, not stored records — so a group costs the same at 10 cells or 10 million."""

    cases: int = 0
    score_sum: float = 0.0
    passed: int = 0
    errors: int = 0

    def add(self, record: ResultRecord) -> None:
        score, ok = cell_summary(record)
        self.cases += 1
        self.score_sum += score
        self.passed += ok
        self.errors += record.error is not None

    def result(self) -> dict[str, Any]:
        n = self.cases
        return {
            "cases": n,
            "score": round(self.score_sum / n, 6) if n else 0.0,
            "pass_rate": round(self.passed / n, 6) if n else 0.0,
            "errors": self.errors,
        }


class Aggregator:
    """Accumulates run aggregates one record at a time.

    The engine feeds records here as they complete and then discards them, so
    a run's memory does not grow with the number of cells. :func:`aggregate`
    is this same class fed from a list, so there is one implementation of the
    arithmetic rather than two that can drift apart.
    """

    def __init__(self) -> None:
        self._overall = _RunningStats()
        self._groups: dict[tuple[str, str], _RunningStats] = {}

    def add(self, record: ResultRecord) -> None:
        self._overall.add(record)
        group = self._groups.setdefault((record.variant, record.model), _RunningStats())
        group.add(record)

    def result(self) -> dict[str, Any]:
        return {
            "overall": self._overall.result(),
            "matrix": [
                {"variant": variant, "model": model, **stats.result()}
                for (variant, model), stats in sorted(self._groups.items())
            ],
        }


def aggregate(records: list[ResultRecord]) -> dict[str, Any]:
    """Run-level aggregates: overall stats plus one entry per variant×model cell group."""
    aggregator = Aggregator()
    for record in records:
        aggregator.add(record)
    return aggregator.result()


def filter_failures(records: Iterable[ResultRecord]) -> list[ResultRecord]:
    """Records whose cell did not pass (errored, unscored, or failed criteria).

    Takes any iterable so a large run can be streamed straight from disk.
    """
    return [record for record in records if not cell_summary(record)[1]]


def compare_aggregates(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Structured diff of two runs' aggregates (a → b).

    Core logic shared by the CLI's compare rendering and the MCP
    ``compare_runs`` tool: per-cell-group deltas, non-overlapping groups, and
    the overall movement.
    """
    groups_a = {(cell["variant"], cell["model"]): cell for cell in a.get("matrix", [])}
    groups_b = {(cell["variant"], cell["model"]): cell for cell in b.get("matrix", [])}

    cells = []
    for variant, model in sorted(set(groups_a) & set(groups_b)):
        cell_a, cell_b = groups_a[(variant, model)], groups_b[(variant, model)]
        cells.append(
            {
                "variant": variant,
                "model": model,
                "score_a": cell_a["score"],
                "score_b": cell_b["score"],
                "score_delta": round(cell_b["score"] - cell_a["score"], 6),
                "pass_rate_a": cell_a["pass_rate"],
                "pass_rate_b": cell_b["pass_rate"],
                "pass_rate_delta": round(cell_b["pass_rate"] - cell_a["pass_rate"], 6),
            }
        )

    overall_a, overall_b = a["overall"], b["overall"]
    return {
        "cells": cells,
        "only_a": [{"variant": v, "model": m} for v, m in sorted(set(groups_a) - set(groups_b))],
        "only_b": [{"variant": v, "model": m} for v, m in sorted(set(groups_b) - set(groups_a))],
        "overall": {
            "score_a": overall_a["score"],
            "score_b": overall_b["score"],
            "score_delta": round(overall_b["score"] - overall_a["score"], 6),
            "pass_rate_a": overall_a["pass_rate"],
            "pass_rate_b": overall_b["pass_rate"],
            "pass_rate_delta": round(overall_b["pass_rate"] - overall_a["pass_rate"], 6),
        },
    }


def selection_note(meta_a: dict[str, Any], meta_b: dict[str, Any]) -> str | None:
    """Warn when two runs did not evaluate the same cases.

    Every delta a comparison shows is attributed to whatever changed between
    the runs — the prompt, the model, the config. That reading is only correct
    if both runs covered the same cases. A sampled run compared against a full
    one, or two samples drawn with different seeds, breaks it silently: the
    numbers stay plausible and part of every delta is just which cases each
    run happened to draw.
    """
    a, b = meta_a.get("selection"), meta_b.get("selection")
    if not a and not b:
        return None
    if not a or not b:
        sampled, whole = (meta_b, meta_a) if not a else (meta_a, meta_b)
        return (
            f"run {sampled['id']} evaluated a sample of {sampled['selection']['sample']} cases "
            f"and run {whole['id']} evaluated all of them, so these runs cover different "
            "cases. Part of every delta below is that difference, not the change you made."
        )
    if (a.get("sample"), a.get("seed")) != (b.get("sample"), b.get("seed")):
        return (
            f"the two runs drew different samples ({a['sample']} cases with seed {a['seed']} "
            f"against {b['sample']} with seed {b['seed']}), so they cover different cases. "
            "Re-run with the same --sample and --sample-seed to compare like with like."
        )
    return None


@dataclass
class GateResult:
    passed: bool
    checks: list[dict[str, Any]]


def evaluate_gate(
    thresholds: Thresholds,
    overall: dict[str, Any],
    baseline_overall: dict[str, Any] | None = None,
) -> GateResult | None:
    """Evaluate configured thresholds against a run's overall aggregates.

    Returns None when no thresholds apply. Baseline checks fail when the run
    is worse than the baseline on score or pass rate.
    """
    checks: list[dict[str, Any]] = []

    if thresholds.min_pass_rate is not None:
        ok = overall["pass_rate"] >= thresholds.min_pass_rate
        checks.append(
            {
                "name": "min_pass_rate",
                "passed": ok,
                "detail": f"pass rate {overall['pass_rate']:.2%} vs required "
                f"{thresholds.min_pass_rate:.2%}",
            }
        )
    if thresholds.min_score is not None:
        ok = overall["score"] >= thresholds.min_score
        checks.append(
            {
                "name": "min_score",
                "passed": ok,
                "detail": f"score {overall['score']:.3f} vs required {thresholds.min_score:.3f}",
            }
        )
    if baseline_overall is not None:
        score_ok = overall["score"] >= baseline_overall["score"]
        rate_ok = overall["pass_rate"] >= baseline_overall["pass_rate"]
        checks.append(
            {
                "name": "baseline",
                "passed": score_ok and rate_ok,
                "detail": f"score {overall['score']:.3f} vs baseline "
                f"{baseline_overall['score']:.3f}; pass rate {overall['pass_rate']:.2%} "
                f"vs baseline {baseline_overall['pass_rate']:.2%}",
            }
        )

    if not checks:
        return None
    return GateResult(passed=all(check["passed"] for check in checks), checks=checks)
