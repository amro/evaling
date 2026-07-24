from evaling.config import Thresholds
from evaling.scoring import aggregate, cell_summary, evaluate_gate
from evaling.storage import ResultRecord


def record(variant="v1", model="m1", case_id="c1", scores=None, error=None):
    return ResultRecord(
        variant=variant, model=model, case_id=case_id, scores=scores or {}, error=error
    )


def entry(score, passed, weight=1.0):
    return {"score": score, "passed": passed, "weight": weight}


class TestCellSummary:
    def test_weighted_mean_and_all_pass(self):
        rec = record(scores={"acc": entry(1.0, True, 3), "fmt": entry(0.5, True, 1)})
        score, passed = cell_summary(rec)
        assert score == 0.875  # (3*1.0 + 1*0.5) / 4
        assert passed

    def test_any_failed_criterion_fails_cell(self):
        rec = record(scores={"acc": entry(1.0, True), "fmt": entry(0.0, False)})
        assert cell_summary(rec) == (0.5, False)

    def test_error_cell_scores_zero(self):
        assert cell_summary(record(error="boom", scores={"acc": entry(1.0, True)})) == (0.0, False)

    def test_no_scores_fails(self):
        assert cell_summary(record()) == (0.0, False)

    def test_criterion_error_entry_fails_cell(self):
        # a crashed scorer stores an entry without passed=True
        rec = record(scores={"acc": {"weight": 1.0, "error": "scorer blew up", "score": 0.0}})
        score, passed = cell_summary(rec)
        assert not passed


class TestAggregate:
    def test_overall_and_matrix_groups(self):
        records = [
            record("v1", "m1", "c1", scores={"a": entry(1.0, True)}),
            record("v1", "m1", "c2", scores={"a": entry(0.0, False)}),
            record("v2", "m1", "c1", scores={"a": entry(1.0, True)}),
            record("v2", "m1", "c2", scores={"a": entry(1.0, True)}),
        ]
        result = aggregate(records)
        assert result["overall"] == {"cases": 4, "score": 0.75, "pass_rate": 0.75, "errors": 0}
        matrix = {(m["variant"], m["model"]): m for m in result["matrix"]}
        assert matrix[("v1", "m1")]["pass_rate"] == 0.5
        assert matrix[("v2", "m1")]["pass_rate"] == 1.0

    def test_errors_counted(self):
        result = aggregate([record(error="x"), record(case_id="c2", scores={"a": entry(1, True)})])
        assert result["overall"]["errors"] == 1
        assert result["overall"]["pass_rate"] == 0.5

    def test_empty_records(self):
        assert aggregate([])["overall"] == {"cases": 0, "score": 0.0, "pass_rate": 0.0, "errors": 0}


class TestCompareAggregates:
    def test_deltas_and_disjoint_groups(self):
        from evaling.scoring import compare_aggregates

        a = {
            "overall": {"score": 0.9, "pass_rate": 1.0},
            "matrix": [
                {"variant": "v1", "model": "m1", "score": 0.9, "pass_rate": 1.0},
                {"variant": "v2", "model": "m1", "score": 0.8, "pass_rate": 0.9},
            ],
        }
        b = {
            "overall": {"score": 0.7, "pass_rate": 0.8},
            "matrix": [
                {"variant": "v1", "model": "m1", "score": 0.7, "pass_rate": 0.8},
                {"variant": "v3", "model": "m1", "score": 0.5, "pass_rate": 0.5},
            ],
        }
        diff = compare_aggregates(a, b)
        [cell] = diff["cells"]
        assert cell["variant"] == "v1"
        assert cell["score_delta"] == -0.2
        assert cell["pass_rate_delta"] == -0.2
        assert diff["only_a"] == [{"variant": "v2", "model": "m1"}]
        assert diff["only_b"] == [{"variant": "v3", "model": "m1"}]
        assert diff["overall"]["score_delta"] == -0.2

    def test_filter_failures(self):
        from evaling.scoring import filter_failures

        records = [
            record("v1", "m1", "c1", scores={"a": entry(1.0, True)}),
            record("v1", "m1", "c2", scores={"a": entry(0.0, False)}),
            record("v1", "m1", "c3", error="boom"),
        ]
        assert [r.case_id for r in filter_failures(records)] == ["c2", "c3"]


class TestGate:
    def test_no_thresholds_returns_none(self):
        overall = {"score": 0.5, "pass_rate": 0.5}
        assert evaluate_gate(Thresholds(), overall) is None

    def test_min_pass_rate(self):
        thresholds = Thresholds(min_pass_rate=0.9)
        assert evaluate_gate(thresholds, {"score": 1.0, "pass_rate": 0.95}).passed
        gate = evaluate_gate(thresholds, {"score": 1.0, "pass_rate": 0.5})
        assert not gate.passed
        assert gate.checks[0]["name"] == "min_pass_rate"
        assert "50.00%" in gate.checks[0]["detail"]

    def test_min_score(self):
        thresholds = Thresholds(min_score=0.8)
        assert evaluate_gate(thresholds, {"score": 0.85, "pass_rate": 1.0}).passed
        assert not evaluate_gate(thresholds, {"score": 0.7, "pass_rate": 1.0}).passed

    def test_baseline_regression(self):
        thresholds = Thresholds()
        current = {"score": 0.8, "pass_rate": 0.9}
        better_baseline = {"score": 0.9, "pass_rate": 0.9}
        equal_baseline = {"score": 0.8, "pass_rate": 0.9}
        gate = evaluate_gate(thresholds, current, better_baseline)
        assert not gate.passed
        assert gate.checks[0]["name"] == "baseline"
        assert evaluate_gate(thresholds, current, equal_baseline).passed

    def test_combined_checks_all_must_pass(self):
        thresholds = Thresholds(min_pass_rate=0.5, min_score=0.5)
        gate = evaluate_gate(
            thresholds, {"score": 0.9, "pass_rate": 0.4}, {"score": 0.1, "pass_rate": 0.1}
        )
        assert not gate.passed
        assert [c["passed"] for c in gate.checks] == [False, True, True]
