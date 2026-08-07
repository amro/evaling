import asyncio
from pathlib import Path

import pytest

from evaling.config import Case
from evaling.scorers.agreement import AgreementScorer
from evaling.scorers.base import ScoringError
from evaling.scorers.python_scorer import PythonScorer
from helpers import loop_ticks_during

BASE = Path(".")


def score(scorer, output, case=None):
    return asyncio.run(scorer.score(output, case or Case()))


def write_scorer(tmp_path, body, name="my_scorer.py"):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return tmp_path


class TestPython:
    def test_bool_return(self, tmp_path):
        base = write_scorer(tmp_path, "def score(output, case):\n    return output == 'yes'\n")
        scorer = PythonScorer({"file": "my_scorer.py"}, base)
        assert score(scorer, "yes").passed
        assert not score(scorer, "no").passed

    def test_numeric_return_with_pass_at(self, tmp_path):
        base = write_scorer(tmp_path, "def score(output, case):\n    return 0.7\n")
        assert not score(PythonScorer({"file": "my_scorer.py"}, base), "x").passed
        scorer = PythonScorer({"file": "my_scorer.py", "pass_at": 0.5}, base)
        result = score(scorer, "x")
        assert result.passed and result.score == 0.7

    def test_mapping_return(self, tmp_path):
        body = (
            "def score(output, case):\n"
            "    return {'score': 0.9, 'passed': True, 'detail': 'solid'}\n"
        )
        result = score(PythonScorer({"file": "my_scorer.py"}, write_scorer(tmp_path, body)), "x")
        assert result.score == 0.9 and result.passed and result.detail == "solid"

    def test_case_dict_available(self, tmp_path):
        body = "def score(output, case):\n    return output == case['vars']['want']\n"
        scorer = PythonScorer({"file": "my_scorer.py"}, write_scorer(tmp_path, body))
        assert score(scorer, "hi", Case(vars={"want": "hi"})).passed

    def test_async_function_supported(self, tmp_path):
        body = "async def score(output, case):\n    return True\n"
        scorer = PythonScorer({"file": "my_scorer.py"}, write_scorer(tmp_path, body))
        assert score(scorer, "x").passed

    def test_sync_function_does_not_block_the_loop(self, tmp_path):
        # Regression: a sync scorer doing real work (a subprocess, an HTTP
        # call) ran on the event loop and stalled every in-flight model call.
        body = "import time\ndef score(output, case):\n    time.sleep(0.2)\n    return True\n"
        scorer = PythonScorer({"file": "my_scorer.py"}, write_scorer(tmp_path, body))
        ticks, result = asyncio.run(loop_ticks_during(scorer.score("x", Case())))
        assert result.passed
        assert ticks >= 3

    def test_custom_function_name(self, tmp_path):
        body = "def grade(output, case):\n    return True\n"
        scorer = PythonScorer(
            {"file": "my_scorer.py", "function": "grade"}, write_scorer(tmp_path, body)
        )
        assert score(scorer, "x").passed

    def test_missing_file_fails_at_construction(self, tmp_path):
        with pytest.raises(ScoringError, match="file not found"):
            PythonScorer({"file": "ghost.py"}, tmp_path)

    def test_missing_function_fails_at_construction(self, tmp_path):
        base = write_scorer(tmp_path, "x = 1\n")
        with pytest.raises(ScoringError, match="no function 'score'"):
            PythonScorer({"file": "my_scorer.py"}, base)

    def test_import_error_fails_at_construction(self, tmp_path):
        base = write_scorer(tmp_path, "raise RuntimeError('bad module')\n")
        with pytest.raises(ScoringError, match="error importing"):
            PythonScorer({"file": "my_scorer.py"}, base)

    def test_raising_function_wrapped(self, tmp_path):
        body = "def score(output, case):\n    raise ValueError('nope')\n"
        scorer = PythonScorer({"file": "my_scorer.py"}, write_scorer(tmp_path, body))
        with pytest.raises(ScoringError, match="raised ValueError"):
            score(scorer, "x")

    def test_out_of_range_number_rejected(self, tmp_path):
        base = write_scorer(tmp_path, "def score(output, case):\n    return 5\n")
        with pytest.raises(ScoringError, match=r"expected a score in \[0, 1\]"):
            score(PythonScorer({"file": "my_scorer.py"}, base), "x")

    def test_out_of_range_mapping_score_rejected(self, tmp_path):
        # Regression: the mapping branch skipped the [0,1] check, letting an
        # unnormalized score inflate aggregates and flip min_score gates.
        body = "def score(output, case):\n    return {'score': 5, 'passed': True}\n"
        scorer = PythonScorer({"file": "my_scorer.py"}, write_scorer(tmp_path, body))
        with pytest.raises(ScoringError, match=r"expected a score in \[0, 1\]"):
            score(scorer, "x")

    def test_bad_return_type_rejected(self, tmp_path):
        base = write_scorer(tmp_path, "def score(output, case):\n    return 'great'\n")
        with pytest.raises(ScoringError, match="returned str"):
            score(PythonScorer({"file": "my_scorer.py"}, base), "x")

    def test_direct_scoreresult_with_nan_rejected(self, tmp_path):
        # Regression: a ScoreResult built by the user's scorer bypassed range
        # validation; NaN corrupted aggregates and broke JSON exports.
        body = (
            "from evaling.scorers.base import ScoreResult\n"
            "def score(output, case):\n"
            "    return ScoreResult(score=float('nan'), passed=True)\n"
        )
        scorer = PythonScorer({"file": "my_scorer.py"}, write_scorer(tmp_path, body))
        with pytest.raises(ScoringError, match=r"finite number in \[0, 1\]"):
            score(scorer, "x")

    def test_direct_scoreresult_out_of_range_rejected(self, tmp_path):
        body = (
            "from evaling.scorers.base import ScoreResult\n"
            "def score(output, case):\n"
            "    return ScoreResult(score=5.0, passed=True)\n"
        )
        scorer = PythonScorer({"file": "my_scorer.py"}, write_scorer(tmp_path, body))
        with pytest.raises(ScoringError, match=r"finite number in \[0, 1\]"):
            score(scorer, "x")

    def test_user_scoring_error_not_double_wrapped(self, tmp_path):
        body = (
            "from evaling.scorers.base import ScoringError\n"
            "def score(output, case):\n"
            "    raise ScoringError('custom clean message')\n"
        )
        scorer = PythonScorer({"file": "my_scorer.py"}, write_scorer(tmp_path, body))
        with pytest.raises(ScoringError, match="^custom clean message$"):
            score(scorer, "x")


class TestAgreement:
    def test_exact_agreement_with_judge_json(self):
        scorer = AgreementScorer({}, BASE)
        result = score(scorer, '{"score": 4, "rationale": "good"}', Case(human_label=4))
        assert result.passed and "predicted=4" in result.detail
        assert not score(scorer, '{"score": 2}', Case(human_label=4)).passed

    def test_exact_normalizes_numeric_strings(self):
        scorer = AgreementScorer({}, BASE)
        assert score(scorer, '{"score": "4"}', Case(human_label=4.0)).passed

    def test_plain_text_output_compared_directly(self):
        scorer = AgreementScorer({}, BASE)
        assert score(scorer, "GOOD", Case(human_label="good")).passed

    def test_within_tolerance(self):
        scorer = AgreementScorer({"mode": "within", "tolerance": 1}, BASE)
        assert score(scorer, '{"score": 4}', Case(human_label=5)).passed
        assert not score(scorer, '{"score": 3}', Case(human_label=5)).passed

    @pytest.mark.parametrize("tolerance", [-1, float("inf"), float("nan")])
    def test_a_nonsense_tolerance_is_refused_at_construction(self, tolerance):
        """Each of these produced a plausible score instead of an error.

        A negative tolerance makes identical values disagree, so a perfect
        judge calibrates at 0%. Infinity makes everything agree, so any judge
        calibrates at 100%. NaN fails every comparison. Silent wrong answers,
        from the tool whose job is measuring agreement.
        """
        with pytest.raises(ScoringError, match="finite and non-negative"):
            AgreementScorer({"mode": "within", "tolerance": tolerance}, BASE)

    def test_the_label_nan_agrees_with_itself(self):
        """ "nan" is a label, not a number: normalizing it made it self-unequal."""
        scorer = AgreementScorer({}, BASE)
        assert score(scorer, '{"score": "nan"}', Case(human_label="nan")).passed

    def test_within_requires_numbers(self):
        scorer = AgreementScorer({"mode": "within"}, BASE)
        with pytest.raises(ScoringError, match="needs numeric"):
            score(scorer, '{"score": "high"}', Case(human_label=5))

    def test_custom_field(self):
        scorer = AgreementScorer({"field": "rating"}, BASE)
        assert score(scorer, '{"rating": 3}', Case(human_label=3)).passed

    def test_missing_field_raises(self):
        with pytest.raises(ScoringError, match="no 'score' field"):
            score(AgreementScorer({}, BASE), '{"grade": 3}', Case(human_label=3))

    def test_missing_human_label_raises(self):
        with pytest.raises(ScoringError, match="human_label"):
            score(AgreementScorer({}, BASE), '{"score": 3}', Case())

    def test_unknown_mode_rejected_at_construction(self):
        with pytest.raises(ScoringError, match="unknown mode"):
            AgreementScorer({"mode": "fuzzy"}, BASE)

    def test_bool_verdict_never_agrees_with_numeric_label(self):
        # Regression: True == 1.0 in Python, so a judge emitting true/false
        # silently "agreed" with 1/0 labels instead of surfacing the mismatch.
        scorer = AgreementScorer({}, BASE)
        assert not score(scorer, '{"score": true}', Case(human_label=1)).passed
        assert not score(scorer, '{"score": false}', Case(human_label=0)).passed
        assert score(scorer, '{"score": true}', Case(human_label=True)).passed

    def test_within_rejects_bool_values(self):
        scorer = AgreementScorer({"mode": "within", "tolerance": 1}, BASE)
        with pytest.raises(ScoringError, match="needs numeric"):
            score(scorer, '{"score": true}', Case(human_label=1))
