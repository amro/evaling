"""`evaling init`: write a runnable example project."""

from pathlib import Path

from evaling.errors import EvalingError

EVAL_YAML = """\
# evaling quickstart — `evaling run` works immediately using the built-in
# deterministic mock provider (no API keys, no network).
#
# To evaluate real models, swap the model entries, e.g.:
#   - id: claude-sonnet-5
#     provider: anthropic          # uses ANTHROPIC_API_KEY
#   - id: gpt-5.2
#     provider: openai             # uses OPENAI_API_KEY

models:
  - id: mock
    provider: mock

variants:
  - name: concise
    prompt: prompts/concise.yaml
  - name: detailed
    prompt: prompts/detailed.yaml

cases:
  # Three flagged card transactions. Each case's `expected` is the detail that
  # should decide it, quoted from that transaction — so the scorecard measures
  # whether the model cited the right signal, not just whether it guessed the
  # right label. It also runs offline: the mock provider echoes the prompt back
  # instead of answering, and the detail is in the prompt.
  file: cases.jsonl

scorecard:
  - criterion: cites-the-signal
    scorer: {type: contains}       # output must contain the case's `expected`

thresholds:
  min_pass_rate: 0.5               # `evaling run` exits non-zero below this
"""

CONCISE_YAML = """\
- role: system
  content: >-
    You review card transactions for fraud. Answer in one sentence: the verdict,
    then the single detail that decided it, quoted from the transaction.
- role: user
  content: "{{ transaction }}"
"""

DETAILED_YAML = """\
- role: system
  content: You review card transactions for fraud.
- role: user
  content: |
    Is this transaction fraud or legitimate? Work through the signals one at a
    time, then finish by quoting the single detail that decided it.

    {{ transaction }}
"""

#: One case per line: `id` names the case, `expected` is what the scorecard's
#: `contains` scorer looks for, and everything else is a template variable.
#:
#: One task over three cases, which is the shape of a real eval. Each
#: `expected` is quoted from its own transaction and appears in no other, so a
#: model that cites the wrong signal fails — the scorecard is not satisfied by
#: guessing a label. That it also passes offline is a consequence: the mock
#: provider echoes the prompt, and the deciding detail is in the prompt.
CASES_JSONL = """\
{"id": "midnight-watch", "transaction": "$2,480 at Luxe Watches Ltd, 03:14 local time, \
card not present, first purchase from this merchant", "expected": "card not present"}
{"id": "weekly-grocery", "transaction": "$63.19 at Fairview Grocery, 18:22 local time, \
chip read, same store weekly for two years", "expected": "chip read"}
{"id": "retry-burst", "transaction": "$1,204 at Nova Electronics, 02:41 local time, \
six declined attempts in the previous minute", "expected": "six declined attempts"}
"""

GITIGNORE = """\
# evaling run history, response cache, and reports
.evaling/

# secrets — never commit these
.evaling.secrets.yaml
"""

SECRETS_EXAMPLE = """\
# Copy to .evaling.secrets.yaml (already gitignored) and fill in.
# Real environment variables always win over this file, so CI keeps working.
#
# ANTHROPIC_API_KEY: sk-ant-...
# OPENAI_API_KEY: sk-...
# GEMINI_API_KEY: ...
"""

FILES = {
    "eval.yaml": EVAL_YAML,
    ".gitignore": GITIGNORE,
    ".evaling.secrets.yaml.example": SECRETS_EXAMPLE,
    "prompts/concise.yaml": CONCISE_YAML,
    "prompts/detailed.yaml": DETAILED_YAML,
    "cases.jsonl": CASES_JSONL,
}


#: Model block per provider, so `init --provider X` scaffolds something real.
MODEL_BLOCKS = {
    "mock": """models:
  - id: mock
    provider: mock
""",
    "anthropic": """models:
  # Reads ANTHROPIC_API_KEY from the environment or .evaling.secrets.yaml
  - id: claude-sonnet-5
    provider: anthropic
    params: {max_tokens: 1024}
""",
    "openai": """models:
  # Reads OPENAI_API_KEY from the environment or .evaling.secrets.yaml
  - id: gpt-5.2
    provider: openai
""",
    "openai-compatible": """models:
  # Any OpenAI-format endpoint: Ollama, vLLM, LM Studio, OpenRouter, Gemini.
  - id: llama3.1:8b
    provider: openai-compatible
    base_url: http://localhost:11434/v1
""",
}


def scaffold_project(
    root: Path, *, force: bool = False, provider: str = "mock"
) -> list[tuple[str, str]]:
    """Write the example files under root; refuse to clobber without force.

    Returns ``(action, name)`` per file. ".gitignore" is merged rather than
    replaced, so reporting it as created would describe the wrong thing.
    """
    files = dict(FILES)
    if provider != "mock":
        files["eval.yaml"] = EVAL_YAML.replace(MODEL_BLOCKS["mock"], MODEL_BLOCKS[provider])

    existing = [name for name in files if (root / name).exists()]
    if existing and not force:
        raise EvalingError(
            f"refusing to overwrite existing file(s): {', '.join(existing)} "
            "(use --force to overwrite)"
        )
    # Read before anything is written. A .gitignore evaling cannot decode used
    # to raise mid-loop, after eval.yaml had already been replaced — a
    # traceback and a half-written scaffold.
    gitignore = _existing_gitignore(root / ".gitignore")

    written = []
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if name == ".gitignore" and gitignore is not None:
            # Compared as bytes, and skipped when they match: reading with
            # universal newlines and writing with "\n" rewrites a CRLF file
            # that already has every entry, so saying "left alone" while
            # changing every line ending would be false.
            merged = _merged_gitignore(gitignore.decode("utf-8")).encode("utf-8")
            if merged == gitignore:
                written.append(("left alone", name))
                continue
            path.write_bytes(merged)
            written.append(("updated", name))
            continue
        path.write_text(content, encoding="utf-8", newline="\n")
        written.append(("created", name))
    return written


def _existing_gitignore(path: Path) -> bytes | None:
    """The file's bytes, or None if there is nothing to merge into."""
    if not path.is_file():
        return None
    raw = path.read_bytes()
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvalingError(
            f"{path} is not valid UTF-8 (byte {raw[exc.start]:#04x} at offset {exc.start}), "
            "so evaling cannot merge its entries in. Re-save it as UTF-8, or add "
            "`.evaling/` and `.evaling.secrets.yaml` to it by hand."
        ) from None
    return raw


def _merged_gitignore(existing: str) -> str:
    """evaling's entries added to a .gitignore, keeping what is already there.

    Every other scaffold file belongs to evaling, so --force replacing it is
    what was asked for. A .gitignore usually predates evaling and covers the
    rest of the repo; replacing it wholesale would drop those entries, and the
    file's whole job is to be complete.
    """
    have = {line.strip() for line in existing.splitlines()}
    missing = [line for line in GITIGNORE.splitlines() if line.strip() and line.strip() not in have]
    if not missing:
        return existing
    if not existing.strip():
        return GITIGNORE
    # Match the file's own convention: appending LF into a CRLF file leaves it
    # mixed, which git tolerates but `core.autocrlf` setups warn about.
    newline = "\r\n" if "\r\n" in existing else "\n"
    separator = "" if existing.endswith(newline) else newline
    return existing + separator + newline + newline.join(missing) + newline
