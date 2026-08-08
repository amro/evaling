import asyncio
from pathlib import Path

import pytest

from evaling.config import Case
from evaling.scorers.base import MAX_JSON_STARTS, ScoringError, parse_json_lenient
from evaling.scorers.builtin import (
    ContainsScorer,
    ExactScorer,
    JsonSchemaScorer,
    JsonValidScorer,
    NotContainsScorer,
    RegexScorer,
)

BASE = Path(".")


def score(scorer, output, case=None):
    return asyncio.run(scorer.score(output, case or Case()))


class TestExact:
    def test_pass_against_expected(self):
        result = score(ExactScorer({}, BASE), "4", Case(expected="4"))
        assert result.passed and result.score == 1.0 and result.detail is None

    def test_fail_includes_expected(self):
        result = score(ExactScorer({}, BASE), "5", Case(expected="4"))
        assert not result.passed and result.score == 0.0
        assert "'4'" in result.detail

    def test_value_param_overrides_expected(self):
        assert score(ExactScorer({"value": "yes"}, BASE), "yes", Case(expected="no")).passed

    def test_strips_by_default_and_can_be_disabled(self):
        assert score(ExactScorer({}, BASE), "  4\n", Case(expected="4")).passed
        assert not score(ExactScorer({"strip": False}, BASE), " 4", Case(expected="4")).passed

    def test_case_insensitive_option(self):
        assert score(ExactScorer({"case_sensitive": False}, BASE), "OK", Case(expected="ok")).passed

    def test_missing_expected_raises(self):
        with pytest.raises(ScoringError, match="expected"):
            score(ExactScorer({}, BASE), "x", Case())


class TestContains:
    def test_pass_and_fail(self):
        assert score(ContainsScorer({}, BASE), "warm places", Case(expected="warm")).passed
        assert not score(ContainsScorer({}, BASE), "cold", Case(expected="warm")).passed

    def test_not_contains_inverts(self):
        assert score(NotContainsScorer({"value": "sorry"}, BASE), "sure!").passed
        assert not score(NotContainsScorer({"value": "sorry"}, BASE), "sorry, no").passed

    def test_case_insensitive(self):
        assert score(ContainsScorer({"case_sensitive": False, "value": "OK"}, BASE), "ok!").passed


class TestRegex:
    def test_search_semantics(self):
        assert score(RegexScorer({"pattern": r"\d{3}"}, BASE), "code 404 here").passed
        assert not score(RegexScorer({"pattern": r"^\d+$"}, BASE), "code 404").passed

    def test_missing_pattern_raises_at_construction(self):
        with pytest.raises(ScoringError, match="pattern"):
            RegexScorer({}, BASE)

    def test_invalid_pattern_raises_at_construction(self):
        with pytest.raises(ScoringError, match="invalid pattern"):
            RegexScorer({"pattern": "("}, BASE)

    def test_case_insensitive(self):
        assert score(RegexScorer({"pattern": "ok", "case_sensitive": False}, BASE), "OK").passed


class TestJson:
    def test_valid_json_passes(self):
        assert score(JsonValidScorer({}, BASE), '{"a": 1}').passed

    def test_invalid_json_fails_with_detail(self):
        result = score(JsonValidScorer({}, BASE), "{nope")
        assert not result.passed and "invalid JSON" in result.detail

    def test_markdown_fences_tolerated(self):
        assert score(JsonValidScorer({}, BASE), '```json\n{"a": 1}\n```').passed
        assert parse_json_lenient("```\n[1, 2]\n```") == [1, 2]

    @pytest.mark.parametrize(
        "text",
        [
            'Sure, here is my evaluation:\n```json\n{"score": 1}\n```',
            '```json\n{"score": 1}```',  # closing fence glued to content
            '```json\n{"score": 1}\n```\nHope that helps!',
            'The verdict is {"score": 1} as requested.',  # no fence at all
            'Bad brace { first, then {"score": 1}.',
        ],
    )
    def test_json_extracted_from_prose_and_fence_variants(self, text):
        # Regression: only exact fence-line/json/fence-line used to parse;
        # judges that add a preamble caused spurious criterion failures.
        assert parse_json_lenient(text) == {"score": 1}

    def test_a_bracket_flood_is_bounded(self):
        """Each start can scan the whole remaining text, so trying every one
        is quadratic — 16 KB of "[" took five seconds and held a concurrency
        slot. The budget was untested: removing it kept the suite green."""
        import json as json_module
        import time

        started = time.perf_counter()
        with pytest.raises(json_module.JSONDecodeError):
            parse_json_lenient("[" * 20_000)
        assert time.perf_counter() - started < 2.0, "the start budget is not being applied"

    def test_json_past_the_budget_is_not_found(self):
        """The bound is a real limit, not a formality — worth stating.

        Output with the answer past the 64th bracket is pathological, and
        finding it would cost every other cell the scan.
        """
        import json as json_module

        buried = "[" * (MAX_JSON_STARTS + 5) + '{"score": 1}'
        with pytest.raises(json_module.JSONDecodeError):
            parse_json_lenient(buried)
        assert parse_json_lenient("[" * (MAX_JSON_STARTS - 2) + '{"score": 1}') == {"score": 1}

    def test_unparseable_text_raises_original_error(self):
        import json as json_module

        with pytest.raises(json_module.JSONDecodeError):
            parse_json_lenient("no json anywhere")

    def test_schema_pass_and_violation(self):
        schema = {
            "type": "object",
            "required": ["answer"],
            "properties": {"answer": {"type": "string"}},
        }
        scorer = JsonSchemaScorer({"schema": schema}, BASE)
        assert score(scorer, '{"answer": "yes"}').passed
        result = score(scorer, '{"answer": 5}')
        assert not result.passed and "schema violation" in result.detail

    def test_schema_from_file(self, tmp_path):
        (tmp_path / "s.json").write_text('{"type": "array"}', encoding="utf-8")
        scorer = JsonSchemaScorer({"schema": "s.json"}, tmp_path)
        assert score(scorer, "[1]").passed
        assert not score(scorer, '{"a": 1}').passed

    def test_schema_file_missing_raises(self, tmp_path):
        with pytest.raises(ScoringError, match="schema file not found"):
            JsonSchemaScorer({"schema": "ghost.json"}, tmp_path)

    def test_invalid_schema_raises(self):
        with pytest.raises(ScoringError, match="invalid schema"):
            JsonSchemaScorer({"schema": {"type": "not-a-type"}}, BASE)
