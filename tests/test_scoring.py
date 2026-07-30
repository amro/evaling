from evaling.config import Thresholds
from evaling.scoring import (
    aggregate,
    cell_summary,
    compare_aggregates,
    evaluate_gate,
    selection_note,
)
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


class TestTheOverallBlockOfAComparison:
    """The summary line people actually read, and nothing asserted it.

    Mutation testing found this: flipping `b - a` to `b + a` in the overall
    pass-rate delta survived the entire suite, as did renaming the key. The
    per-cell deltas were covered; the overall block they roll up to was not.
    """

    def aggregates(self, score, pass_rate):
        return {
            "overall": {"cases": 2, "score": score, "pass_rate": pass_rate, "errors": 0},
            "matrix": [
                {
                    "variant": "v",
                    "model": "m",
                    "cases": 2,
                    "score": score,
                    "pass_rate": pass_rate,
                    "errors": 0,
                }
            ],
        }

    def test_the_overall_deltas_are_b_minus_a(self):
        diff = compare_aggregates(self.aggregates(0.25, 0.5), self.aggregates(0.75, 1.0))
        assert diff["overall"]["score_delta"] == 0.5
        assert diff["overall"]["pass_rate_delta"] == 0.5

    def test_a_regression_is_negative(self):
        """Addition would give the same magnitude with the wrong sign."""
        diff = compare_aggregates(self.aggregates(0.75, 1.0), self.aggregates(0.25, 0.5))
        assert diff["overall"]["score_delta"] == -0.5
        assert diff["overall"]["pass_rate_delta"] == -0.5

    def test_the_overall_block_carries_both_sides(self):
        diff = compare_aggregates(self.aggregates(0.25, 0.5), self.aggregates(0.75, 1.0))
        assert diff["overall"] == {
            "score_a": 0.25,
            "score_b": 0.75,
            "score_delta": 0.5,
            "pass_rate_a": 0.5,
            "pass_rate_b": 1.0,
            "pass_rate_delta": 0.5,
        }

    def test_no_change_is_zero_not_a_sum(self):
        diff = compare_aggregates(self.aggregates(0.5, 0.5), self.aggregates(0.5, 0.5))
        assert diff["overall"]["score_delta"] == 0.0
        assert diff["overall"]["pass_rate_delta"] == 0.0

    def test_a_run_with_no_matrix_does_not_crash(self):
        """`.get("matrix", [])` has a default for a reason."""
        bare = {"overall": {"cases": 0, "score": 0.0, "pass_rate": 0.0, "errors": 0}}
        diff = compare_aggregates(bare, bare)
        assert diff["cells"] == []


class TestTheDifferentPopulationsWarning:
    """Same draw parameters over different-sized case sets.

    A seed selects by position, so the same `--sample 5 --sample-seed 7` over
    40 cases and over 400 picks different cases entirely. Mutation testing
    found this branch untested.
    """

    def meta(self, run_id, sample, seed, available):
        return {"id": run_id, "selection": {"sample": sample, "seed": seed, "available": available}}

    def test_the_same_draw_over_different_populations_is_flagged(self):
        note = selection_note(self.meta("a", 5, 7, 40), self.meta("b", 5, 7, 400))
        assert note is not None
        assert "different sets of cases" in note
        assert "40" in note and "400" in note

    def test_the_same_draw_over_the_same_population_is_not(self):
        assert selection_note(self.meta("a", 5, 7, 40), self.meta("b", 5, 7, 40)) is None

    def test_it_explains_why_a_seed_is_not_enough(self):
        note = selection_note(self.meta("a", 5, 7, 40), self.meta("b", 5, 7, 400))
        assert "position" in note


class TestTheGateBoundary:
    """`>=`, not `>`: a run that exactly meets its threshold passes.

    Mutation testing found `min_score`'s boundary untested — every existing
    case sat comfortably above or below it. A threshold you cannot exactly
    meet fails a run for hitting its target, and `min_score: 0.8` against a
    score of 0.8 is the case a user is most likely to actually hit.
    """

    def test_a_score_exactly_at_the_minimum_passes(self):
        gate = evaluate_gate(Thresholds(min_score=0.8), {"score": 0.8, "pass_rate": 1.0})
        assert gate.passed

    def test_a_pass_rate_exactly_at_the_minimum_passes(self):
        gate = evaluate_gate(Thresholds(min_pass_rate=0.9), {"score": 1.0, "pass_rate": 0.9})
        assert gate.passed

    def test_a_score_just_below_the_minimum_fails(self):
        gate = evaluate_gate(Thresholds(min_score=0.8), {"score": 0.7999, "pass_rate": 1.0})
        assert not gate.passed

    def test_a_baseline_exactly_matched_is_not_a_regression(self):
        same = {"score": 0.5, "pass_rate": 0.5}
        assert evaluate_gate(Thresholds(baseline="regression"), same, dict(same)).passed


class TestWhatAFailedCheckReports:
    """`name` and `detail` are the machine-readable contract.

    CI reads them out of `--json run`, so renaming either key silently breaks
    every pipeline consuming it. Nothing asserted their presence.
    """

    def test_each_check_names_the_threshold_it_evaluated(self):
        gate = evaluate_gate(
            Thresholds(min_pass_rate=0.9, min_score=0.8), {"score": 0.1, "pass_rate": 0.1}
        )
        assert [check["name"] for check in gate.checks] == ["min_pass_rate", "min_score"]
        assert all(check["passed"] is False for check in gate.checks)

    def test_a_detail_states_the_measurement_against_the_requirement(self):
        gate = evaluate_gate(Thresholds(min_score=0.8), {"score": 0.25, "pass_rate": 1.0})
        [check] = gate.checks
        assert check["detail"] == "score 0.250 vs required 0.800"

    def test_a_pass_rate_detail_is_a_percentage(self):
        gate = evaluate_gate(Thresholds(min_pass_rate=0.9), {"score": 1.0, "pass_rate": 0.5})
        [check] = gate.checks
        assert check["detail"] == "pass rate 50.00% vs required 90.00%"

    def test_a_baseline_check_reports_both_measures(self):
        gate = evaluate_gate(
            Thresholds(baseline="regression"),
            {"score": 0.4, "pass_rate": 0.4},
            {"score": 0.8, "pass_rate": 0.8},
        )
        [check] = gate.checks
        assert check["name"] == "baseline"
        assert (
            check["detail"] == "score 0.400 vs baseline 0.800; pass rate 40.00% vs baseline 80.00%"
        )


class TestARunThatEvaluatedNothing:
    """Zero cells is no verdict, not a failing one.

    A run's aggregates report `pass_rate: 0.0` when no cell ran, which is a
    "no data" fallback rather than a measurement — but a threshold read it as
    "0% of cases passed" and failed the run. A `--max-cost` ceiling reached
    before the first call therefore announced a quality regression it had
    never measured.
    """

    EMPTY = {"cases": 0, "score": 0.0, "pass_rate": 0.0, "errors": 0}

    def test_no_cells_means_no_gate(self):
        assert evaluate_gate(Thresholds(min_pass_rate=0.5), self.EMPTY) is None

    def test_not_even_against_a_score_threshold(self):
        assert evaluate_gate(Thresholds(min_score=0.8), self.EMPTY) is None

    def test_not_even_against_a_baseline(self):
        baseline = {"cases": 4, "score": 0.9, "pass_rate": 0.9, "errors": 0}
        assert evaluate_gate(Thresholds(baseline="regression"), self.EMPTY, baseline) is None

    def test_one_cell_is_still_judged(self):
        """The boundary: a run that did something is a run with a verdict."""
        ran = {"cases": 1, "score": 0.0, "pass_rate": 0.0, "errors": 0}
        gate = evaluate_gate(Thresholds(min_pass_rate=0.5), ran)
        assert gate is not None and not gate.passed


class TestComparingAgainstARunThatRecordedNoCaseList:
    """An older run, or one from before the digest existed.

    `_different_cases` compares a digest of the cases each run covered. When
    one side has none it cannot conclude anything, and the honest answer is
    silence rather than a warning about a difference it did not observe.
    Mutation testing flipped the guard's `or` to `and`, which makes a
    one-sided comparison either crash or invent a warning, and nothing
    noticed.
    """

    def meta(self, run_id, cases=None):
        entry = {"id": run_id, "matrix": {}}
        if cases is not None:
            entry["matrix"]["cases"] = cases
        return entry

    def test_no_warning_when_only_one_side_recorded_its_cases(self):
        assert selection_note(self.meta("a", "digest-1"), self.meta("b")) is None

    def test_no_warning_when_only_the_other_side_did(self):
        assert selection_note(self.meta("a"), self.meta("b", "digest-1")) is None

    def test_no_warning_when_neither_did(self):
        assert selection_note(self.meta("a"), self.meta("b")) is None

    def test_a_warning_when_both_recorded_and_they_differ(self):
        note = selection_note(self.meta("a", "digest-1"), self.meta("b", "digest-2"))
        assert note is not None
        assert "different sets of cases" in note

    def test_no_warning_when_both_recorded_the_same(self):
        assert selection_note(self.meta("a", "same"), self.meta("b", "same")) is None
