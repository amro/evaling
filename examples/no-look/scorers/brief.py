"""A scorer is the redaction boundary: it sees the data, and emits a verdict.

In no-look mode this detail string is the only thing about the output that
reaches a report — so it must say what went wrong without quoting the case.
"""


def score(output: str, case: dict):
    words = len(output.split())
    if words > 60:
        return {"score": 0.0, "passed": False, "detail": f"too long: {words} words"}
    if not output.strip():
        return {"score": 0.0, "passed": False, "detail": "empty response"}
    return {"score": 1.0, "passed": True, "detail": f"{words} words"}
