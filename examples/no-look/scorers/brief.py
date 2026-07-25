"""A scorer is the redaction boundary: it sees the data, and emits a verdict.

In no-look mode this detail string is the only thing about the output that
reaches a report — so it must say what went wrong without quoting the case.
"""

WORD_LIMIT = 60


def score(output: str, case: dict):
    if not output.strip():
        return {"score": 0.0, "passed": False, "detail": "empty response"}
    words = len(output.split())
    if words > WORD_LIMIT:
        return {
            "score": 0.0,
            "passed": False,
            "detail": f"over the {WORD_LIMIT}-word limit by {words - WORD_LIMIT}",
        }
    return {"score": 1.0, "passed": True, "detail": f"within the {WORD_LIMIT}-word limit"}
