"""The scorer interface: grade one model output for one case."""

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evaling.config.schema import Case
from evaling.errors import EvalingError


class ScoringError(EvalingError):
    """A scorer could not produce a result."""


@dataclass(frozen=True)
class ScoreResult:
    """A normalized grade: score in [0, 1], an explicit pass/fail, optional detail."""

    score: float
    passed: bool
    detail: str | None = None


class Scorer(ABC):
    """One instance per scorecard criterion, reused across all cells."""

    def __init__(self, params: dict[str, Any], base_dir: Path):
        self.params = params
        self.base_dir = base_dir

    @abstractmethod
    async def score(self, output: str, case: Case) -> ScoreResult:
        """Grade one output. Raise ScoringError when grading is impossible."""


def parse_json_lenient(text: str) -> Any:
    """Parse JSON, tolerating the markdown code fences models love to add."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        lines = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        stripped = "\n".join(lines).strip()
    return json.loads(stripped)
