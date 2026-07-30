"""Agreement scorers: how well an autorater's verdict matches a human label.

Used for judge calibration (meta-evals): the output under test is a judge's
JSON verdict; the case's ``human_label`` is ground truth. Params:

- ``mode``: ``exact`` (default) or ``within`` (numeric distance).
- ``tolerance``: max |predicted - label| for ``within`` (default 0).
- ``field``: key holding the predicted value when the output is a JSON mapping
  (default ``score``).
"""

import json
from numbers import Real

from evaling.config.schema import Case
from evaling.scorers.base import Scorer, ScoreResult, ScoringError, parse_json_lenient


class AgreementScorer(Scorer):
    def __init__(self, params, base_dir):
        super().__init__(params, base_dir)
        self.mode = params.get("mode", "exact")
        if self.mode not in ("exact", "within"):
            raise ScoringError(f"agreement scorer: unknown mode {self.mode!r} (exact|within)")
        raw = params.get("tolerance", 0)
        try:
            self.tolerance = float(raw)
        except (TypeError, ValueError):
            # ScorerSpec allows extra keys, so the schema accepts anything
            # here; a bare ValueError from float() is not caught by the CLI
            # and came out as a traceback.
            raise ScoringError(
                f"agreement scorer: tolerance must be a number, got {raw!r}"
            ) from None
        self.field = params.get("field", "score")

    async def score(self, output: str, case: Case) -> ScoreResult:
        if case.human_label is None:
            raise ScoringError("agreement scorer requires the case's 'human_label'")
        predicted = self._extract(output)
        label = case.human_label
        detail = f"predicted={predicted!r} label={label!r}"

        if self.mode == "within":
            if (
                isinstance(predicted, bool)
                or isinstance(label, bool)
                or not isinstance(predicted, Real)
                or not isinstance(label, Real)
            ):
                raise ScoringError(f"agreement 'within' needs numeric values ({detail})")
            passed = abs(float(predicted) - float(label)) <= self.tolerance
        elif isinstance(predicted, bool) != isinstance(label, bool):
            # bool == 1.0 in Python; a judge emitting true/false must not
            # silently "agree" with numeric labels
            passed = False
        else:
            passed = self._normalize(predicted) == self._normalize(label)
        return ScoreResult(1.0 if passed else 0.0, passed, detail)

    def _extract(self, output: str):
        try:
            data = parse_json_lenient(output)
        except json.JSONDecodeError:
            return output.strip()
        if isinstance(data, dict):
            if self.field not in data:
                raise ScoringError(f"agreement scorer: output JSON has no {self.field!r} field")
            return data[self.field]
        return data

    @staticmethod
    def _normalize(value):
        # 4 == 4.0 == "4" should agree in exact mode
        if isinstance(value, bool):
            return value
        if isinstance(value, Real):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                return value.strip().lower()
        return value
