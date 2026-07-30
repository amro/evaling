"""The scorer interface: grade one model output for one case."""

import json
import math
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
    """A normalized grade: score in [0, 1], an explicit pass/fail, optional detail.

    The bounds are enforced at construction: an out-of-range or non-finite
    score (e.g. NaN from a custom scorer) would otherwise corrupt run
    aggregates, flip threshold gates, and emit invalid JSON exports.
    """

    score: float
    passed: bool
    detail: str | None = None

    def __post_init__(self):
        score = self.score
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
            or not 0.0 <= float(score) <= 1.0
        ):
            raise ScoringError(f"score must be a finite number in [0, 1], got {score!r}")


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
    for index in _candidate_starts(stripped):
        try:
            value, _ = decoder.raw_decode(stripped, index)
            return value
        except json.JSONDecodeError:
            continue

    raise first_error


#: How many embedded-JSON starts to try. Each attempt can scan the whole
#: remaining text, so trying every one is quadratic: 16 KB of "[" took five
#: seconds, and a megabyte of it would hold a concurrency slot for hours.
#: Model output with the real answer past the 64th bracket does not exist.
MAX_JSON_STARTS = 64


def _candidate_starts(text: str) -> "list[int]":
    """Positions worth attempting a parse from, newest-first budget applied."""
    starts = []
    for index, char in enumerate(text):
        if char in "{[":
            starts.append(index)
            if len(starts) >= MAX_JSON_STARTS:
                break
    return starts
