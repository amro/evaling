"""Does the answer name the document it came from?

Declining counts as cited: there is no source to name when the corpus does not
cover the question, and marking that a failure would make honesty score worse
than confabulation — the opposite of what this eval is for.
"""

import re

DECLINES = ("i don't know", "don't cover", "not covered", "no information")
CITATION = re.compile(r"\[(refunds|shipping|accounts|warranty)\]")


def score(output: str, case: dict):
    text = output or ""
    if any(phrase in text.lower() for phrase in DECLINES):
        return {"score": 1.0, "passed": True, "detail": "declined — nothing to cite"}
    found = CITATION.search(text)
    return {
        "score": 1.0 if found else 0.0,
        "passed": bool(found),
        "detail": f"cited {found.group(1)}" if found else "no source named",
    }
