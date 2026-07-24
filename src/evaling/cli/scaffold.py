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
  file: cases.jsonl

scorecard:
  - criterion: accuracy
    scorer: {type: contains}       # output must contain the case's `expected`

thresholds:
  min_pass_rate: 0.5               # `evaling run` exits non-zero below this
"""

CONCISE_YAML = """\
- role: system
  content: Answer in one short sentence.
- role: user
  content: "{{ question }}"
"""

DETAILED_YAML = """\
- role: system
  content: Answer thoroughly, step by step.
- role: user
  content: |
    Please answer in detail: {{ question }}
"""

CASES_JSONL = """\
{"id": "sky", "question": "What color is the sky on a clear day?", "expected": "sky"}
{"id": "capital", "question": "What is the capital of France?", "expected": "France"}
{"id": "arithmetic", "question": "What is 2 + 2?", "expected": "2"}
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


def scaffold_project(root: Path, *, force: bool = False) -> list[str]:
    """Write the example files under root; refuse to clobber without force."""
    existing = [name for name in FILES if (root / name).exists()]
    if existing and not force:
        raise EvalingError(
            f"refusing to overwrite existing file(s): {', '.join(existing)} "
            "(use --force to overwrite)"
        )
    created = []
    for name, content in FILES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        created.append(name)
    return created
