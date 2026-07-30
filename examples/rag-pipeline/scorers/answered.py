"""Did the pipeline answer correctly — including by declining when it should?

Two different questions in one criterion, because for a retrieval system they
are the same question: does the answer reflect what was actually retrieved?

- Answerable case: the expected fact has to appear.
- Unanswerable case: an admission has to appear, and inventing a confident
  answer is the failure. A pipeline that is right four times and confabulates
  twice is not 66% good; it is unusable, and this is where that shows.
"""

DECLINES = ("i don't know", "don't cover", "not covered", "no information")


def score(output: str, case: dict):
    text = (output or "").lower()
    declined = any(phrase in text for phrase in DECLINES)

    if not case["vars"]["answerable"]:
        return {
            "score": 1.0 if declined else 0.0,
            "passed": declined,
            "detail": (
                "declined, correctly — the corpus does not cover this"
                if declined
                else "answered a question the documents do not cover"
            ),
        }

    expected = str(case["expected"]).lower()
    found = expected in text
    if declined:
        return {"score": 0.0, "passed": False, "detail": "declined a question the corpus answers"}
    return {
        "score": 1.0 if found else 0.0,
        "passed": found,
        "detail": "found the expected fact" if found else f"expected to see {expected!r}",
    }
