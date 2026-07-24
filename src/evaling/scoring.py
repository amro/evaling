"""Scorecard aggregation and threshold gating.

Per cell: the weighted mean of criterion scores; a cell passes only if every
criterion passed. Cells that errored (no output) score 0 and fail — they count
against pass rates rather than disappearing from them.
"""

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


def _group_stats(records: list[ResultRecord]) -> dict[str, Any]:
    summaries = [cell_summary(record) for record in records]
    n = len(summaries)
    passed = sum(1 for _, ok in summaries if ok)
    return {
        "cases": n,
        "score": round(sum(score for score, _ in summaries) / n, 6) if n else 0.0,
        "pass_rate": round(passed / n, 6) if n else 0.0,
        "errors": sum(1 for record in records if record.error is not None),
    }


def aggregate(records: list[ResultRecord]) -> dict[str, Any]:
    """Run-level aggregates: overall stats plus one entry per variant×model cell group."""
    groups: dict[tuple[str, str], list[ResultRecord]] = {}
    for record in records:
        groups.setdefault((record.variant, record.model), []).append(record)
    return {
        "overall": _group_stats(records),
        "matrix": [
            {"variant": variant, "model": model, **_group_stats(group)}
            for (variant, model), group in sorted(groups.items())
        ],
    }


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
