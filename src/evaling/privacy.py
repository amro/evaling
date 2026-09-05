"""No-look mode: evaluate data nobody is allowed to read afterwards.

The use case is validating a prompt or a model against production traffic in a
setting where a human looking at that traffic is the thing being prevented. The
scores are the deliverable; the data is not.

Record redaction happens in :func:`redact_record`, called by the engine the
moment a cell finishes scoring and before the record reaches anything else.
Source failures have a separate engine boundary in ``sources.source_errors``:
they abort the run without producing a cell record. Downstream surfaces do
not need to remember to scrub either path themselves.

What survives: variant, model, case id, per-criterion scores and pass/fail,
token counts, cost, latency, and whether a cell errored. None of that contains
case content.
"""

import hashlib
from typing import Any

from evaling.secrets import redact
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


def scrub_secrets(record: ResultRecord, values: "list[str] | tuple[str, ...]") -> ResultRecord:
    """Remove known credentials from anything a record carries. In place.

    Unlike no-look this is always on. A model can return a credential —
    a gateway echoing a header, a `command` wrapper printing its own
    environment — and `results.jsonl` is shared, exported, and attached to
    issues. Only errors were covered before, so the guarantee in
    ``HttpProvider._redact`` ("never let a secret reach an error message, a
    log, or results.jsonl") held for two of the three.
    """
    if not values:
        return record
    if record.output:
        record.output = redact(record.output, values)
    if record.error:
        record.error = redact(record.error, values)
    for entry in record.scores.values():
        for field in ("detail", "error"):
            if isinstance(entry.get(field), str):
                entry[field] = redact(entry[field], values)
    return record


def redact_record(
    record: ResultRecord,
    keep_detail: frozenset[str] = frozenset(),
    *,
    hash_case_ids: bool = False,
) -> ResultRecord:
    """Strip everything derived from case content. Modifies in place.

    ``keep_detail`` names the criteria whose ``detail`` may survive — the
    criteria scored by a user-supplied Python function, whose author decides
    what is safe to emit. Everything else goes, because a scorer that evaluates
    case content tends to quote it when explaining itself: a failing
    ``contains`` reports the ``expected`` value it was looking for, an
    ``agreement`` scorer reports the label and the extracted verdict, and a
    judge's rationale quotes the text it graded.

    A criterion's ``error`` goes unconditionally. It is an exception message
    from a scorer that had the output in its hands.
    """
    record.messages = []
    record.output = None
    if hash_case_ids and record.case_id:
        record.case_id = hash_case_id(record.case_id)
    for criterion, entry in record.scores.items():
        if criterion not in keep_detail:
            entry.pop("detail", None)
        # Scorer errors quote what the scorer was looking at.
        if "error" in entry:
            entry["error"] = "scorer failed — detail withheld (no-look)"
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
