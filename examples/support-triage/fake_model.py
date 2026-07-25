#!/usr/bin/env python3
"""A deterministic stand-in for a real model, so this example runs offline.

Reads an evaling request on stdin, writes a response on stdout. The behaviour
is crude on purpose: it gets the easy tickets right, mishandles a few, and
returns malformed JSON once — so the scorecard has something to find and the
report is worth looking at.

Point `provider: command` at any script with this shape to evaluate something
that isn't an HTTP chat API: an agent, a RAG pipeline, a local binary.
"""

import json
import sys

CATEGORIES = {
    "refund": ["refund", "money back", "charged twice", "reimburse"],
    "bug": ["error", "crash", "broken", "not working", "500"],
    "account": ["password", "log in", "login", "locked out", "2fa"],
    "billing": ["invoice", "receipt", "vat", "subscription", "plan"],
}
# A vague prompt gets the obvious signals only; a prompt that spells out what
# "urgent" means gets the rest. This is the difference the eval is measuring.
URGENT_OBVIOUS = ["urgent", "asap"]
URGENT_FULL = URGENT_OBVIOUS + ["immediately", "outage", "production", "losing"]


def classify(text: str, urgency_defined: bool) -> dict:
    lowered = text.lower()
    category = "other"
    for name, words in CATEGORIES.items():
        if any(word in lowered for word in words):
            category = name
            break
    signals = URGENT_FULL if urgency_defined else URGENT_OBVIOUS
    urgency = "high" if any(word in lowered for word in signals) else "normal"
    return {"category": category, "urgency": urgency}


def main() -> None:
    request = json.load(sys.stdin)
    # Only the last user turn: the system prompt names every category, so
    # classifying the whole conversation would just match the instructions.
    user_turns = [m for m in request["messages"] if m["role"] == "user"]
    text = " ".join(
        part.get("text", "")
        for part in (user_turns[-1]["parts"] if user_turns else [])
        if part.get("type") == "text"
    )
    system = " ".join(
        part.get("text", "")
        for message in request["messages"]
        if message["role"] == "system"
        for part in message["parts"]
        if part.get("type") == "text"
    )
    verdict = classify(text, urgency_defined="Urgency is" in system)

    style = request.get("params", {}).get("style", "clean")
    if style == "chatty":
        # A model that ignores "JSON only" and wraps its answer in prose —
        # the failure mode a json-valid criterion exists to catch.
        body = f"Sure! Here's the triage:\n```json\n{json.dumps(verdict)}\n```"
    elif style == "sloppy" and verdict["category"] == "other":
        body = "{category: other, urgency: " + verdict["urgency"] + "}"  # not valid JSON
    else:
        body = json.dumps(verdict)

    words = len(text.split())
    print(
        json.dumps(
            {
                "text": body,
                "input_tokens": words,
                "output_tokens": len(body.split()),
            }
        )
    )


if __name__ == "__main__":
    main()
