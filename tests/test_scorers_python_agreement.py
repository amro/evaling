import asyncio
from pathlib import Path

import pytest

from evaling.config import Case
from evaling.scorers.agreement import AgreementScorer
from evaling.scorers.base import ScoringError
from evaling.scorers.python_scorer import PythonScorer

BASE = Path(".")


def score(scorer, output, case=None):
    return asyncio.run(scorer.score(output, case or Case()))


def write_scorer(tmp_path, body, name="my_scorer.py"):
    path = tmp_path / name
    path.write_text(body)
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

    def test_bad_return_type_rejected(self, tmp_path):
        base = write_scorer(tmp_path, "def score(output, case):\n    return 'great'\n")
        with pytest.raises(ScoringError, match="returned str"):
            score(PythonScorer({"file": "my_scorer.py"}, base), "x")


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
