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
        # A numpy bool, or a bare 1, reaches records and exports as a non-bool
        # and reads as a different type downstream than every other cell's.
        if not isinstance(self.passed, bool):
            raise ScoringError(f"passed must be a bool, got {self.passed!r}")


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

    # json decodes containers recursively, so input nested deeper than the
    # interpreter's stack raises RecursionError. That is not a JSONDecodeError,
    # so it bypasses every caller's handling and surfaces the interpreter's
    # message. Every parse attempt below can raise it, not only the first:
    # deeply nested JSON is most likely in exactly the decorated output the
    # fallbacks exist for.
    def attempt(parse):
        try:
            return True, parse()
        except json.JSONDecodeError as exc:
            return False, exc
        except RecursionError:
            return False, json.JSONDecodeError("JSON is nested too deeply to parse", stripped, 0)

    ok, result = attempt(lambda: json.loads(stripped))
    if ok:
        return result
    first_error = result

    fence = _FENCED_BLOCK.search(stripped)
    if fence:
        ok, result = attempt(lambda: json.loads(fence.group(1).strip()))
        if ok:
            return result

    decoder = json.JSONDecoder()
    for index in _candidate_starts(stripped):
        ok, result = attempt(lambda index=index: decoder.raw_decode(stripped, index)[0])
        if ok:
            return result

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
