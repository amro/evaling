"""Report models a provider offers that the price table does not price.

WHAT THIS CANNOT DO: check whether a price is *correct*. Rates live only in
HTML rate cards, and scraping a number into the table would put a wrong price
where an honest "unknown" was — the failure that under-counts spend. Comparing
rates stays a human step, in RELEASING.md. This checks coverage, nothing else.

WHY COVERAGE IS WORTH CHECKING: a model absent from the table reports an
*unknown* cost rather than a stale one, so a run using it cannot be budgeted
and `--max-cost` cannot hold. New models appear between releases; four had
accumulated by 2026-09-04.

The provider model lists are structured APIs, not pages, so this part is
reliable. The judgement it cannot make is which listed models evaling should
price at all — the lists carry image, audio, embedding and research models
that the table excludes on purpose. NOT_TEXT filters the obvious ones and
`known-unpriced.txt` records the rest as decided, so a rerun is quiet until
something genuinely new appears.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

IGNORE_FILE = Path(__file__).resolve().parent / "known-unpriced.txt"

#: Substrings marking a model evaling cannot call or deliberately omits.
NOT_TEXT = (
    "embedding",
    "image",
    "audio",
    "tts",
    "whisper",
    "realtime",
    "moderation",
    "dall-e",
    "sora",
    "veo",
    "imagen",
    "computer-use",
    "deep-research",
    "aqa",
    "transcribe",
    "speech",
    "codex-mini",
    "guard",
    "embedgemma",
    "learnlm",
    # Categorically not text generation, so no judgement is involved.
    "lyria",
    "nano-banana",
    "robotics",
    "omni",
    "-live-",
    "live-translate",
    "antigravity",
)


def _get(url: str, headers: dict[str, str]) -> dict:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed hosts
        return json.load(response)


def anthropic(key: str) -> list[str]:
    data = _get(
        "https://api.anthropic.com/v1/models?limit=100",
        {"x-api-key": key, "anthropic-version": "2023-06-01"},
    )
    return [m["id"] for m in data.get("data", [])]


def openai(key: str) -> list[str]:
    data = _get("https://api.openai.com/v1/models", {"Authorization": f"Bearer {key}"})
    return [m["id"] for m in data.get("data", [])]


def gemini(key: str) -> list[str]:
    # Header rather than ?key=, so the credential stays out of anything that
    # logs a URL.
    data = _get(
        "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000",
        {"x-goog-api-key": key},
    )
    return [m["name"].split("/")[-1] for m in data.get("models", [])]


PROVIDERS = {
    "anthropic": ("ANTHROPIC_API_KEY", anthropic),
    "openai": ("OPENAI_API_KEY", openai),
    "gemini": ("GEMINI_API_KEY", gemini),
}


#: A dated snapshot pins one release of a base model, e.g.
#: claude-opus-4-5-20251101 or gpt-4o-2024-05-13.
SNAPSHOT_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$|-\d{6,8}$")


def is_text_model(model_id: str) -> bool:
    """Whether an id looks like something evaling could call and price."""
    return not any(marker in model_id.lower() for marker in NOT_TEXT)


def base_id(model_id: str) -> str:
    """A dated snapshot reduced to the model it pins; anything else unchanged.

    Lookup is by exact id, so a snapshot is not priced by its base — but the
    table deliberately carries base ids, and reporting every dated variant
    separately would bury the finding. So a snapshot counts as covered when its
    base is, and is reported under the base name when it is not.
    """
    return SNAPSHOT_SUFFIX.sub("", model_id)


def unpriced(listed: list[str], priced: set[str], ignored: set[str]) -> list[str]:
    """Models offered but neither priced nor deliberately skipped.

    Snapshots collapse onto their base, so a new dated release of an unpriced
    model is reported once under the name you would add to the table.
    """
    known = priced | ignored
    missing = {
        base_id(m)
        for m in listed
        if is_text_model(m) and m not in known and base_id(m) not in known
    }
    return sorted(missing)


def load_ignored() -> set[str]:
    if not IGNORE_FILE.exists():
        return set()
    return {
        line.split("#", 1)[0].strip()
        for line in IGNORE_FILE.read_text(encoding="utf-8").splitlines()
        if line.split("#", 1)[0].strip()
    }


def main(argv: list[str] | None = None) -> int:
    from evaling.providers.pricing import PRICES, PRICING_AS_OF

    # In CI a deleted or expired secret would otherwise disable the check
    # silently: every run green, nobody reading the log.
    require_all = "--require-all" in (argv if argv is not None else sys.argv[1:])

    priced, ignored = set(PRICES), load_ignored()
    print(f"price table: {len(priced)} models, checked against the cards on {PRICING_AS_OF}")
    print("this checks coverage only — it cannot tell you a rate is wrong\n")

    findings, skipped = {}, []
    for name, (env_var, fetch) in PROVIDERS.items():
        key = os.environ.get(env_var)
        if not key:
            skipped.append(f"{name} (no {env_var})")
            continue
        try:
            listed = fetch(key)
        except Exception as exc:  # noqa: BLE001 - a provider being down is not a finding
            skipped.append(f"{name} ({type(exc).__name__})")
            continue
        missing = unpriced(listed, priced, ignored)
        print(f"{name}: {len(listed)} listed, {len(missing)} unpriced")
        if missing:
            findings[name] = missing

    if skipped:
        print("\nnot checked: " + ", ".join(skipped))

    if findings:
        print("\nModels offered but not priced — each reports an unknown cost:\n")
        for name, missing in findings.items():
            for model in missing:
                print(f"  {name}: {model}")
        print(
            "\nPrice them from the rate card (RELEASING.md step 1), or add them to "
            f"{IGNORE_FILE.name} if evaling should not price them."
        )
        return 1
    if skipped and require_all:
        print(
            "\nFailing because a provider could not be checked and --require-all is set. "
            "A missing key disables coverage for that provider entirely."
        )
        return 1
    if skipped and not findings:
        print("\nnothing missing among the providers that were checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
