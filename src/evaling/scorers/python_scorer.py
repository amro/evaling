"""User-supplied Python scoring functions.

Params: ``file`` (path to a .py file, relative to the config) and ``function``
(name inside it, default "score"). The function receives ``(output: str,
case: dict)`` and may return a bool, a number in [0, 1], or a mapping with
``score``/``passed``/``detail``. It may be sync or async.
"""

import importlib.util
import inspect
from numbers import Real

from evaling.config.schema import Case
from evaling.scorers.base import Scorer, ScoreResult, ScoringError


class PythonScorer(Scorer):
    def __init__(self, params, base_dir):
        super().__init__(params, base_dir)
        file = params.get("file")
        if not isinstance(file, str) or not file:
            raise ScoringError("python scorer requires a 'file' param")
        path = (base_dir / file).resolve()
        if not path.is_file():
            raise ScoringError(f"python scorer: file not found: {path}")
        function = params.get("function", "score")

        spec = importlib.util.spec_from_file_location(f"evaling_scorer_{path.stem}", path)
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            raise ScoringError(f"python scorer: error importing {path}: {exc}") from exc
        fn = getattr(module, function, None)
        if not callable(fn):
            raise ScoringError(f"python scorer: no function {function!r} in {path}")
        self.fn = fn

    async def score(self, output: str, case: Case) -> ScoreResult:
        try:
            result = self.fn(output, case.model_dump())
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            raise ScoringError(f"python scorer raised {type(exc).__name__}: {exc}") from exc
        return self._coerce(result)

    def _coerce(self, result) -> ScoreResult:
        if isinstance(result, ScoreResult):
            return result
        if isinstance(result, bool):
            return ScoreResult(1.0 if result else 0.0, result)
        if isinstance(result, Real):
            value = float(result)
            if not 0.0 <= value <= 1.0:
                raise ScoringError(f"python scorer returned {value}, expected a score in [0, 1]")
            return ScoreResult(value, value >= float(self.params.get("pass_at", 1.0)))
        if isinstance(result, dict) and isinstance(result.get("score"), Real):
            value = float(result["score"])
            if not 0.0 <= value <= 1.0:
                raise ScoringError(f"python scorer returned {value}, expected a score in [0, 1]")
            passed = result.get("passed", value >= float(self.params.get("pass_at", 1.0)))
            if not isinstance(passed, bool):
                raise ScoringError("python scorer mapping 'passed' must be a bool")
            return ScoreResult(value, passed, result.get("detail"))
        raise ScoringError(
            f"python scorer returned {type(result).__name__}; expected bool, number in [0, 1], "
            "or a mapping with 'score'"
        )
