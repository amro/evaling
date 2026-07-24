"""evaling: compare prompt variants and models from the command line."""

__version__ = "0.1.0"

from evaling.engine import RunResult, run_eval, run_eval_async  # noqa: E402

__all__ = ["RunResult", "__version__", "run_eval", "run_eval_async"]
