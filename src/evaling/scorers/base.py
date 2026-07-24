"""The scorer interface: grade one model output for one case."""

import json
import re
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


_FENCED_BLOCK = re.compile(r"```[\w-]*[ \t]*\n?(.*?)```", re.DOTALL)


def parse_json_lenient(text: str) -> Any:
    """Parse JSON out of model output, tolerating common decorations.

    Handles, in order: clean JSON; a fenced ```json block (with or without
    surrounding prose); the first balanced object/array embedded in prose.
    Raises the original JSONDecodeError when nothing parses.
    """
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        first_error = exc

    fence = _FENCED_BLOCK.search(stripped)
    if fence:
        try:
            return json.loads(fence.group(1).strip())
        except json.JSONDecodeError:
            pass

    decoder = json.JSONDecoder()
    for index, char in enumerate(stripped):
        if char in "{[":
            try:
                value, _ = decoder.raw_decode(stripped, index)
                return value
            except json.JSONDecodeError:
                continue

    raise first_error
