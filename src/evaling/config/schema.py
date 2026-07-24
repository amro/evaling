"""Pydantic schema for eval.yaml.

Unknown keys are rejected everywhere except scorer parameters, so config typos
fail loudly at load time.
"""

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

ProviderName = Literal["anthropic", "openai", "openai-compatible", "command", "mock"]

ScorerType = Literal[
    "exact",
    "contains",
    "not-contains",
    "regex",
    "json-valid",
    "json-schema",
    "llm-judge",
    "python",
    "agreement",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Settings(StrictModel):
    """Workspace settings: machine/user concerns, resolvable in layers."""

    output_dir: Path = Path(".evaling/runs")
    cache_dir: Path = Path(".evaling/cache")
    concurrency: int = Field(default=8, ge=1)
    cache: bool = True


class TextPart(StrictModel):
    text: str


class ImagePart(StrictModel):
    image: str


class FilePart(StrictModel):
    file: str


class AudioPart(StrictModel):
    audio: str


class VideoPart(StrictModel):
    video: str


ContentPart = TextPart | ImagePart | FilePart | AudioPart | VideoPart


class Message(StrictModel):
    role: Literal["system", "user", "assistant"]
    # A bare string is shorthand for a single text part.
    content: str | list[ContentPart]


class ModelSpec(StrictModel):
    id: str = Field(min_length=1)
    provider: ProviderName
    base_url: str | None = None
    command: str | None = None
    # Which environment variable holds the API key (providers have sensible
    # defaults, e.g. ANTHROPIC_API_KEY; set this for OpenAI-compatible
    # backends like Gemini or OpenRouter with their own key variables).
    api_key_env: str | None = None
    # Per-model request timeout in seconds (wired by HTTP providers).
    timeout_s: float | None = Field(default=None, gt=0)
    # Per-model retry count for transient failures (attempts = retries + 1).
    max_retries: int | None = Field(default=None, ge=0)
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _pricing_override(self) -> "ModelSpec":
        """Validate params.pricing at load time, before any spend.

        A malformed override is worse than no override: a negative rate would
        shrink tracked spend (defeating --max-cost), and a non-numeric one used
        to raise mid-run, after the API call was already billed.
        """
        pricing = self.params.get("pricing")
        if pricing is None:
            return self
        if not isinstance(pricing, dict) or {"input", "output"} - pricing.keys():
            raise ValueError(
                f"model {self.id!r}: params.pricing needs numeric 'input' and 'output' "
                "(USD per million tokens)"
            )
        for field in ("input", "output"):
            value = pricing[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValueError(
                    f"model {self.id!r}: params.pricing.{field} must be a "
                    f"non-negative number, got {value!r}"
                )
        return self

    @model_validator(mode="after")
    def _provider_fields(self) -> "ModelSpec":
        if self.provider == "openai-compatible" and not self.base_url:
            raise ValueError(f"model {self.id!r}: provider 'openai-compatible' requires 'base_url'")
        if self.provider == "command" and not self.command:
            raise ValueError(f"model {self.id!r}: provider 'command' requires 'command'")
        if self.provider != "command" and self.command is not None:
            raise ValueError(f"model {self.id!r}: 'command' is only valid for the command provider")
        return self


class VariantSpec(StrictModel):
    name: str = Field(min_length=1)
    # A string is a path to an external prompt file; a list is inline messages.
    prompt: str | list[Message]


class Case(StrictModel):
    id: str | None = None
    vars: dict[str, Any] = Field(default_factory=dict)
    files: dict[str, str] = Field(default_factory=dict)
    expected: Any = None
    human_label: Any = None


class CaseFileRef(StrictModel):
    file: str


class ScorerSpec(BaseModel):
    """A scorer reference. Extra keys are scorer-specific parameters."""

    model_config = ConfigDict(extra="allow")

    type: ScorerType

    @property
    def params(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class CriterionSpec(StrictModel):
    criterion: str = Field(min_length=1)
    weight: float = Field(default=1.0, gt=0)
    scorer: ScorerSpec


class JudgeSpec(StrictModel):
    model: str = Field(min_length=1)
    # A string is a path to an external rubric prompt file; a list is inline messages.
    rubric: str | list[Message]


class Thresholds(StrictModel):
    min_pass_rate: float | None = Field(default=None, ge=0, le=1)
    min_score: float | None = Field(default=None, ge=0, le=1)
    # "regression" gates against the pinned baseline; a run id gates against that run.
    baseline: str | None = None


class EvalConfig(StrictModel):
    settings: Settings = Field(default_factory=Settings)
    models: list[ModelSpec] = Field(min_length=1)
    variants: list[VariantSpec] = Field(min_length=1)
    cases: CaseFileRef | list[Case]
    scorecard: list[CriterionSpec] = Field(min_length=1)
    judges: dict[str, JudgeSpec] = Field(default_factory=dict)
    thresholds: Thresholds = Field(default_factory=Thresholds)

    # Directory containing the config file; relative paths resolve against it.
    _base_dir: Path = PrivateAttr(default=Path())

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    @model_validator(mode="after")
    def _cross_references(self) -> "EvalConfig":
        _require_unique((m.id for m in self.models), "model id")
        _require_unique((v.name for v in self.variants), "variant name")
        if isinstance(self.cases, list):
            if not self.cases:
                raise ValueError("cases: at least one case is required")
            _require_unique((c.id for c in self.cases if c.id is not None), "case id")

        model_ids = {m.id for m in self.models}
        for name, judge in self.judges.items():
            if judge.model not in model_ids:
                raise ValueError(f"judge {name!r} references unknown model {judge.model!r}")

        for crit in self.scorecard:
            if crit.scorer.type == "llm-judge":
                judge = crit.scorer.params.get("judge")
                if not isinstance(judge, str) or not judge:
                    raise ValueError(
                        f"criterion {crit.criterion!r}: llm-judge scorer requires a 'judge' name"
                    )
                if judge not in self.judges:
                    raise ValueError(
                        f"criterion {crit.criterion!r} references unknown judge {judge!r}"
                    )
        return self


def _require_unique(values: Any, kind: str) -> None:
    seen: set[Any] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"duplicate {kind}: {value!r}")
        seen.add(value)
