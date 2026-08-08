"""User-supplied Python scoring functions.

Params: ``file`` (path to a .py file, relative to the config) and ``function``
(name inside it, default "score"). The function receives ``(output: str,
case: dict)`` and may return a bool, a number in [0, 1], or a mapping with
``score``/``passed``/``detail``. It may be sync or async.
"""

import asyncio
import importlib.util
import inspect
import math
from numbers import Real

from evaling.config.schema import Case
from evaling.errors import EvalingError
from evaling.scorers.base import Scorer, ScoreResult, ScoringError


def _pass_at(params) -> float:
    """The threshold a bare number must reach to pass, validated once."""
    raw = params.get("pass_at", 1.0)
    if isinstance(raw, bool) or not isinstance(raw, (str, Real)):
        raise ScoringError(f"python scorer: 'pass_at' must be a number, got {raw!r}")
    try:
        value = float(raw)
    except ValueError:
        raise ScoringError(f"python scorer: 'pass_at' must be a number, got {raw!r}") from None
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ScoringError(f"python scorer: 'pass_at' must be in [0, 1], got {raw!r}")
    return value


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
        if not isinstance(function, str) or not function:
            raise ScoringError(f"python scorer: 'function' must be a name, got {function!r}")

        # Checked at construction, before any cell runs: an unusable pass_at
        # otherwise resolves per cell inside score(), where the failure is one
        # criterion's error rather than a config problem — a run whose every
        # cell scored 0 still reported "succeeded, 0 errors".
        self.pass_at = _pass_at(params)

        spec = importlib.util.spec_from_file_location(f"evaling_scorer_{path.stem}", path)
        # No loader for anything Python cannot import as a module — a file
        # named .txt, or a path with no suffix. Unchecked, module_from_spec
        # raised AttributeError on None and came out as a traceback.
        if spec is None or spec.loader is None:
            raise ScoringError(
                f"python scorer: {path} cannot be imported as Python. "
                "The file must be a .py module."
            )
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
            if inspect.iscoroutinefunction(self.fn):
                result = await self.fn(output, case.model_dump())
            else:
                # Off-thread: a sync scorer doing real work (I/O, a subprocess)
                # would otherwise stall every in-flight model call.
                result = await asyncio.to_thread(self.fn, output, case.model_dump())
                if inspect.isawaitable(result):
                    result = await result
        except EvalingError:
            raise  # already a clean user-facing message
        except SystemExit as exc:
            # A script adapted into a scorer often still calls sys.exit().
            # That is not a BaseException the run should honour: it ends one
            # criterion, like any other failure inside a scorer.
            code = "" if exc.code is None else repr(exc.code)
            raise ScoringError(f"python scorer called sys.exit({code})") from exc
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
            return ScoreResult(value, value >= self.pass_at)
        if isinstance(result, dict) and isinstance(result.get("score"), Real):
            value = float(result["score"])
            if not 0.0 <= value <= 1.0:
                raise ScoringError(f"python scorer returned {value}, expected a score in [0, 1]")
            passed = result.get("passed", value >= self.pass_at)
            if not isinstance(passed, bool):
                raise ScoringError("python scorer mapping 'passed' must be a bool")
            return ScoreResult(value, passed, result.get("detail"))
        raise ScoringError(
            f"python scorer returned {type(result).__name__}; expected bool, number in [0, 1], "
            "or a mapping with 'score'"
        )
