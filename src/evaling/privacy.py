"""No-look mode: evaluate data nobody is allowed to read afterwards.

The use case is validating a prompt or a model against production traffic in a
setting where a human looking at that traffic is the thing being prevented. The
scores are the deliverable; the data is not.

Redaction happens in exactly one place — :func:`redact_record`, called by the
engine the moment a cell finishes scoring and before the record reaches
anything else. Everything downstream (storage, callbacks, the progress
display, reports, exports, the MCP server) then has nothing to leak. A mode
that instead asked six subsystems to remember to redact would eventually meet
a seventh.

What survives: variant, model, case id, per-criterion scores and pass/fail,
token counts, cost, latency, and whether a cell errored. None of that contains
case content.
"""

import hashlib
from typing import Any

from evaling.storage import ResultRecord

#: Stand-in for a case id when ids are hashed.
HASH_PREFIX = "case-"
HASH_LENGTH = 16


def hash_case_id(case_id: str) -> str:
    """A stable, non-reversible stand-in for an id that is itself identifying.

    Stable across cells and runs, so a case can still be followed through a
    matrix and compared between runs without the original id existing anywhere.
    """
    digest = hashlib.sha256(case_id.encode("utf-8")).hexdigest()
    return f"{HASH_PREFIX}{digest[:HASH_LENGTH]}"


def redact_record(
    record: ResultRecord,
    judge_criteria: frozenset[str] = frozenset(),
    *,
    hash_case_ids: bool = False,
) -> ResultRecord:
    """Strip everything derived from case content. Modifies in place.

    ``judge_criteria`` names the criteria scored by an LLM judge. Their
    ``detail`` is the judge's rationale, which quotes the text it graded, so it
    goes. Details from other scorers are kept: a Python scorer's detail is
    written by the user, who is the right person to decide what is safe to
    emit — which makes the scorer the place where that judgment belongs.
    """
    record.messages = []
    record.output = None
    if hash_case_ids and record.case_id:
        record.case_id = hash_case_id(record.case_id)
    for criterion, entry in record.scores.items():
        if criterion in judge_criteria:
            entry.pop("detail", None)
    return record


def redact_config_snapshot(data: dict[str, Any]) -> dict[str, Any]:
    """Remove inline cases from the stored config snapshot.

    A config that lists cases inline carries the data itself. File- and
    source-backed configs reference their data instead, so only the inline
    form needs this.
    """
    cases = data.get("cases")
    if isinstance(cases, list):
        data = dict(data)
        data["cases"] = {"redacted": len(cases)}
    return data
