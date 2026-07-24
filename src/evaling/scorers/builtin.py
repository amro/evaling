"""Deterministic built-in scorers: exact, contains, regex, JSON checks."""

import json
import re

import jsonschema

from evaling.config.schema import Case
from evaling.scorers.base import Scorer, ScoreResult, ScoringError, parse_json_lenient


def _pass_fail(passed: bool, detail_on_fail: str) -> ScoreResult:
    return ScoreResult(1.0 if passed else 0.0, passed, None if passed else detail_on_fail)


class ExactScorer(Scorer):
    """Output equals the expected value (params: value, case_sensitive, strip)."""

    async def score(self, output: str, case: Case) -> ScoreResult:
        expected = self.params.get("value", case.expected)
        if expected is None:
            raise ScoringError("exact scorer needs the case's 'expected' or a 'value' param")
        got, want = output, str(expected)
        if self.params.get("strip", True):
            got, want = got.strip(), want.strip()
        if not self.params.get("case_sensitive", True):
            got, want = got.lower(), want.lower()
        return _pass_fail(got == want, f"expected {want!r}")


class ContainsScorer(Scorer):
    """Output contains the needle (params: value, case_sensitive); negate inverts."""

    negate = False

    async def score(self, output: str, case: Case) -> ScoreResult:
        needle = self.params.get("value", case.expected)
        if needle is None:
            raise ScoringError(
                f"{'not-contains' if self.negate else 'contains'} scorer needs "
                "the case's 'expected' or a 'value' param"
            )
        haystack, needle = output, str(needle)
        if not self.params.get("case_sensitive", True):
            haystack, needle = haystack.lower(), needle.lower()
        found = needle in haystack
        passed = found != self.negate
        verb = "must not contain" if self.negate else "does not contain"
        return _pass_fail(passed, f"output {verb} {needle!r}")


class NotContainsScorer(ContainsScorer):
    negate = True


class RegexScorer(Scorer):
    """Output matches a pattern (params: pattern, case_sensitive)."""

    def __init__(self, params, base_dir):
        super().__init__(params, base_dir)
        pattern = params.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ScoringError("regex scorer requires a 'pattern' param")
        flags = 0 if params.get("case_sensitive", True) else re.IGNORECASE
        try:
            self.regex = re.compile(pattern, flags)
        except re.error as exc:
            raise ScoringError(f"regex scorer: invalid pattern {pattern!r}: {exc}") from exc

    async def score(self, output: str, case: Case) -> ScoreResult:
        return _pass_fail(
            self.regex.search(output) is not None,
            f"output does not match /{self.regex.pattern}/",
        )


class JsonValidScorer(Scorer):
    """Output parses as JSON (markdown fences tolerated)."""

    async def score(self, output: str, case: Case) -> ScoreResult:
        try:
            parse_json_lenient(output)
        except json.JSONDecodeError as exc:
            return ScoreResult(0.0, False, f"invalid JSON: {exc.msg}")
        return ScoreResult(1.0, True)


class JsonSchemaScorer(Scorer):
    """Output parses as JSON and matches a schema (params: schema — inline or path)."""

    def __init__(self, params, base_dir):
        super().__init__(params, base_dir)
        schema = params.get("schema")
        if isinstance(schema, str):
            path = base_dir / schema
            try:
                schema = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                raise ScoringError(f"json-schema scorer: schema file not found: {path}") from None
            except json.JSONDecodeError as exc:
                message = f"json-schema scorer: {path} is not valid JSON: {exc.msg}"
                raise ScoringError(message) from exc
        if not isinstance(schema, dict):
            raise ScoringError("json-schema scorer needs a 'schema' param (mapping or file path)")
        try:
            jsonschema.Draft202012Validator.check_schema(schema)
        except jsonschema.exceptions.SchemaError as exc:
            raise ScoringError(f"json-schema scorer: invalid schema: {exc.message}") from exc
        self.validator = jsonschema.Draft202012Validator(schema)

    async def score(self, output: str, case: Case) -> ScoreResult:
        try:
            data = parse_json_lenient(output)
        except json.JSONDecodeError as exc:
            return ScoreResult(0.0, False, f"invalid JSON: {exc.msg}")
        errors = sorted(self.validator.iter_errors(data), key=str)
        if errors:
            return ScoreResult(0.0, False, f"schema violation: {errors[0].message}")
        return ScoreResult(1.0, True)
