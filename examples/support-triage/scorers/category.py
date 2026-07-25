"""Score the category and urgency the model returned against the expected pair.

Shows the shape of a Python scorer: parse the output yourself, decide what
"correct" means, and return a detail string worth reading in a report.
"""

import json


def _parse(output: str) -> dict | None:
    text = output.strip()
    if text.startswith("```"):  # tolerate a fenced block
        text = text.split("```")[1].removeprefix("json").strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def score(output: str, case: dict):
    parsed = _parse(output)
    if parsed is None:
        return {"score": 0.0, "passed": False, "detail": "output was not JSON"}

    want_category = case["vars"]["expected_category"]
    want_urgency = case["vars"]["expected_urgency"]
    got_category = parsed.get("category")
    got_urgency = parsed.get("urgency")

    hits = (got_category == want_category) + (got_urgency == want_urgency)
    detail = []
    if got_category != want_category:
        detail.append(f"category: expected {want_category}, got {got_category}")
    if got_urgency != want_urgency:
        detail.append(f"urgency: expected {want_urgency}, got {got_urgency}")

    return {
        "score": hits / 2,
        "passed": hits == 2,
        "detail": "; ".join(detail) or "category and urgency both correct",
    }
