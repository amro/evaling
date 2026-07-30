#!/usr/bin/env python3
"""A retrieval-augmented pipeline behind the `command` provider.

This is the shape that makes the `command` provider worth having: the thing
being evaluated is a *system* — retrieve, then answer — not a chat API. evaling
sees one request and one answer; everything between them is yours.

Deterministic and offline, so the example runs anywhere and this file stays
about the plumbing rather than about a model. Swap `synthesize()` for a real
model call and nothing else here changes.

Protocol (the `command` provider's, in full):

    stdin   {"model": ..., "params": {...}, "messages": [{role, parts}, ...]}
    stdout  {"text": "...", "input_tokens": N, "output_tokens": N}

`params` comes from the model block in eval.yaml, which is how the two
"models" below differ: one retrieves a single document, the other three.
The system message is the prompt variant, and this pipeline reads directives
out of it — that is what makes a prompt change a *pipeline* change.
"""

import json
import re
import sys
from pathlib import Path

CORPUS = Path(__file__).parent / "corpus"
#: Below this overlap a document is not really about the question. The
#: grounded prompt refuses at this line; the plain one answers anyway.
RELEVANCE_FLOOR = 0.05

STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "be",
    "can",
    "do",
    "does",
    "for",
    "from",
    "how",
    "i",
    "if",
    "in",
    "is",
    "it",
    "long",
    "many",
    "much",
    "my",
    "of",
    "on",
    "or",
    "the",
    "to",
    "we",
    "what",
    "when",
    "where",
    "will",
    "with",
    "you",
}


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS}


def load_corpus() -> list[tuple[str, str]]:
    return sorted((path.stem, path.read_text(encoding="utf-8")) for path in CORPUS.glob("*.md"))


def retrieve(question: str, top_k: int) -> list[tuple[str, str, float]]:
    """Rank documents by how much of the question they cover.

    Jaccard over word sets — crude on purpose. A real pipeline swaps this for
    embeddings, and the eval around it does not change.
    """
    asked = words(question)
    scored = []
    for doc_id, text in load_corpus():
        overlap = asked & words(text)
        score = len(overlap) / len(asked | words(text)) if asked else 0.0
        # Normalize by the question rather than the union, so a long document
        # is not punished for being long.
        coverage = len(overlap) / len(asked) if asked else 0.0
        scored.append((doc_id, text, round(max(score, coverage * 0.5), 4)))
    scored.sort(key=lambda row: (-row[2], row[0]))
    return scored[:top_k]


def synthesize(question: str, passages: list[tuple[str, str, float]], cite: bool) -> str:
    """Stand-in for the generation step: quote the sentence that answers best.

    A real pipeline calls a model here with the retrieved passages in context.
    The eval does not care which it is — that is the point.

    Candidate sentences come from every retrieved document, not just the top
    one. That is what makes `top_k` a real dimension: at 1, an answer living
    in the second-ranked document is not reachable at all.
    """
    asked = words(question)
    candidates = []
    for doc_id, text, _ in passages:
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            sentence = sentence.strip().replace("\n", " ")
            if sentence and not sentence.startswith("#"):
                candidates.append((len(asked & words(sentence)), doc_id, sentence))
    if not candidates:
        return "I don't know — the documentation doesn't cover that."
    overlap, doc_id, answer = max(candidates, key=lambda row: (row[0], -len(row[2])))
    return f"{answer} [{doc_id}]" if cite else answer


def answer(question: str, system: str, top_k: int) -> str:
    grounded = "only from the documents" in system.lower()
    cite = "cite" in system.lower()

    passages = retrieve(question, top_k)
    if grounded and (not passages or passages[0][2] < RELEVANCE_FLOOR):
        # The behaviour the eval is really measuring: a grounded pipeline
        # declines when retrieval found nothing, instead of answering from
        # whatever came back first.
        return "I don't know — the documentation doesn't cover that."
    return synthesize(question, passages, cite)


def main() -> None:
    request = json.loads(sys.stdin.read())
    system, question = "", ""
    for message in request.get("messages", []):
        text = "".join(part.get("text", "") for part in message.get("parts", []))
        if message.get("role") == "system":
            system += text
        else:
            question += text

    top_k = int(request.get("params", {}).get("top_k", 1))
    text = answer(question, system, top_k)
    # Token counts are what a real pipeline would report from its model call;
    # evaling uses them for the run's totals and for cost when priced.
    json.dump(
        {
            "text": text,
            "input_tokens": len(question) // 4,
            "output_tokens": len(text) // 4,
        },
        sys.stdout,
    )


if __name__ == "__main__":
    main()
