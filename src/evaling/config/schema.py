"""Pydantic schema for eval.yaml.

Unknown keys are rejected everywhere except scorer parameters, so config typos
fail loudly at load time.
"""

import math
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

ModelRole = Literal["candidate", "judge", "both"]
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

    output_dir: Path = Field(default=Path(".evaling/runs"), description="Where runs are stored.")
    cache_dir: Path = Field(
        default=Path(".evaling/cache"), description="Where cached responses are stored."
    )
    concurrency: int = Field(
        default=8, ge=1, description="Cells in flight at once across the whole run."
    )
    cache: bool = Field(
        default=True,
        description="Reuse a stored response when the same request repeats, instead of paying "
        "for it again.",
    )


class TextPart(StrictModel):
    text: str = Field(description="Literal text. Jinja2 expressions are rendered per case.")


class ImagePart(StrictModel):
    image: str = Field(description="Path to an image, relative to the config file.")


class FilePart(StrictModel):
    file: str = Field(description="Path to a document (e.g. a PDF), relative to the config file.")


class AudioPart(StrictModel):
    audio: str = Field(description="Path to an audio file, relative to the config file.")


class VideoPart(StrictModel):
    video: str = Field(description="Path to a video file, relative to the config file.")


ContentPart = TextPart | ImagePart | FilePart | AudioPart | VideoPart


class Message(StrictModel):
    role: Literal["system", "user", "assistant"] = Field(
        description="Who the turn is from. `system` is hoisted where the provider expects it."
    )
    content: str | list[ContentPart] = Field(
        description="A bare string is shorthand for a single text part. Use a list to mix text "
        "with media."
    )


class ModelSpec(StrictModel):
    id: str = Field(
        min_length=1,
        description="Identifier for this model, and the API model name unless `params.model` "
        "overrides it. Must be unique. Pricing is looked up by the effective API model name, so "
        "`params.model` decides it where set.",
    )
    provider: ProviderName = Field(description="Which adapter talks to this model.")
    base_url: str | None = Field(
        default=None,
        description="Endpoint override. Required by `openai-compatible`; ignored elsewhere.",
    )
    command: str | None = Field(
        default=None,
        description="Program to run, as a single shell string. Required by, and only valid for, "
        "the `command` provider. Runs with the config file's directory as its working directory.",
    )
    # Which environment variable holds the API key (providers have sensible
    # defaults, e.g. ANTHROPIC_API_KEY; set this for OpenAI-compatible
    # backends like Gemini or OpenRouter with their own key variables).
    # Constrained to environment-variable shape so a pasted key is caught here
    # rather than committed: this field is the one place a credential can enter
    # a config while looking like it belongs, and the config is serialized
    # verbatim into every run's snapshot.
    api_key_env: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
        description="Name of the environment variable holding the API key. Providers have "
        "defaults (e.g. ANTHROPIC_API_KEY); set this for OpenAI-compatible backends with their "
        "own variable. This is a variable *name*, never a key: the pattern rejects a pasted "
        "credential, and configs are stored verbatim in every run.",
    )
    # Per-model request timeout in seconds. Honoured by the HTTP providers
    # and by `command`, which defaults to 300s rather than 120s.
    timeout_s: float | None = Field(
        default=None,
        gt=0,
        allow_inf_nan=False,
        description="Per-request timeout in seconds. Defaults to 120s for HTTP providers and "
        "300s for `command`.",
    )
    # Per-model retry count for transient failures (attempts = retries + 1).
    max_retries: int | None = Field(
        default=None,
        ge=0,
        description="Retries for transient failures; total attempts are retries + 1.",
    )
    # Cap in-flight calls to this model (composes with settings.concurrency).
    max_concurrency: int | None = Field(
        default=None,
        ge=1,
        description="Cap in-flight calls to this model. Composes with `settings.concurrency`.",
    )
    # Proactive rate limit: requests per rolling minute for this model.
    requests_per_minute: int | None = Field(
        default=None,
        ge=1,
        description="Proactive rate limit for this model, in requests per rolling minute.",
    )
    # What this model is here for. `candidate` (the default) means it is one of
    # the systems under test and gets matrix cells; `judge` means it is only
    # ever called by an llm-judge scorer; `both` is a model you want graded
    # *and* grading. Declaring a judge's model as a candidate used to be
    # implicit, which silently doubled a run and added a row where the judge
    # scored its own output.
    role: ModelRole = Field(
        default="candidate",
        description="What the model is here for. `candidate` is under test and gets matrix "
        "cells; `judge` is only called by an llm-judge scorer and gets none; `both` is graded "
        "and grading. A judge left as `candidate` silently doubles the run.",
    )
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Passed to the provider verbatim (`temperature`, `top_p`, …), so evaling "
        "does not validate them — a parameter a model rejects comes back as a provider error. "
        "Three are read by evaling itself: `model` overrides the API model name, `pricing` "
        "overrides the built-in rate and is validated at load, and `max_tokens` bounds the "
        "pre-run cost estimate.",
    )

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
            bad_type = isinstance(value, bool) or not isinstance(value, (int, float))
            # `value < 0` is False for NaN, and inf clears every
            # comparison — both then reach estimate_cost and fail the
            # cell after the call was billed, which is the exact
            # failure this validator exists to move earlier.
            if bad_type or not math.isfinite(value) or value < 0:
                raise ValueError(
                    f"model {self.id!r}: params.pricing.{field} must be a "
                    f"finite non-negative number, got {value!r}"
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
    name: str = Field(
        min_length=1, description="Identifies this variant in results. Must be unique."
    )
    prompt: str | list[Message] = Field(
        description="A string is a path to a prompt file, relative to the config; a list is "
        "inline messages. Case variables interpolate as {{ name }}."
    )


class Case(StrictModel):
    id: str | None = Field(
        default=None,
        description="Identifies the case in results. Generated if omitted; must be unique.",
    )
    vars: dict[str, Any] = Field(
        default_factory=dict, description="Values interpolated into the prompt."
    )
    files: dict[str, str] = Field(
        default_factory=dict,
        description="Named attachment paths. Relative paths stay under the file that declared "
        "them; only a config may reach outside, and only with an absolute path.",
    )
    expected: Any = Field(
        default=None,
        description="The correct answer. Comparison scorers (`exact`, `contains`, "
        "`not-contains`) use this when their `value` is omitted, which is the usual way to "
        "write them.",
    )
    human_label: Any = Field(
        default=None,
        description="Ground truth for `agreement` scorers, when measuring how well a judge "
        "matches a human.",
    )


class CaseFileRef(StrictModel):
    file: str = Field(description="Path to a .jsonl or .csv of cases, relative to the config file.")


class CaseSourceRef(StrictModel):
    """Cases fetched from user code, a page at a time.

    ``source`` is ``path/to/file.py:factory``; the factory is called with
    ``params`` and returns an object with ``fetch(cursor, limit)``. See
    evaling.sources.
    """

    source: str = Field(
        min_length=1,
        description="`path/to/file.py:factory`, relative to the config. The factory receives "
        "`params` and returns an object with `fetch(cursor, limit)`.",
    )
    params: dict[str, Any] = Field(default_factory=dict, description="Passed to the factory.")
    page_size: int = Field(default=100, gt=0, description="Cases requested per fetch.")
    limit: int | None = Field(
        default=None,
        gt=0,
        description="Stop after this many cases. Omitted means take everything the source has, "
        "which is unbounded and so requires --max-cost.",
    )


class Privacy(StrictModel):
    """Controls for evaluating data that must not be readable afterwards.

    ``no_look`` is the switch that matters: prompts, model outputs, judge
    rationales, and attachments are dropped before anything is written to disk
    or shown, leaving scores, counts, and timings. See docs/no-look.md.
    """

    no_look: bool = Field(
        default=False,
        description="Drop prompts, outputs, judge rationales and attachments before anything is "
        "written or shown, leaving scores, counts and timings.",
    )
    keep_case_ids: bool = Field(
        default=False,
        description="Keep raw case ids in no-look mode. Off by default, because an id from a "
        "production system is often an email or an order number and identifies a record as "
        "surely as the record does. Ids are otherwise hashed, which still lets a case be "
        "followed across a matrix and between runs.",
    )


class ScorerSpec(BaseModel):
    """A scorer reference. Extra keys are scorer-specific parameters."""

    model_config = ConfigDict(extra="allow")

    type: ScorerType = Field(
        description="Which scorer to run. Every other key here is a parameter for that scorer, "
        "and unknown ones pass through unread rather than failing — so a mistyped parameter is "
        "silently ignored."
    )

    @property
    def params(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class CriterionSpec(StrictModel):
    criterion: str = Field(
        min_length=1,
        description="Names this criterion in results. Must be unique within the scorecard.",
    )
    weight: float = Field(
        default=1.0,
        gt=0,
        allow_inf_nan=False,
        description="Relative weight in the overall score.",
    )
    scorer: ScorerSpec = Field(description="The scorer and its parameters.")


class JudgeSpec(StrictModel):
    model: str = Field(
        min_length=1,
        description="Id of the model that grades. It must appear in `models` with role `judge` "
        "or `both`, or it would also be evaluated as a candidate.",
    )
    rubric: str | list[Message] = Field(
        description="A string is a path to a rubric prompt file, relative to the config; a list "
        "is inline messages. The judge must return JSON carrying a numeric verdict."
    )


class Thresholds(StrictModel):
    min_pass_rate: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description="Minimum overall pass rate. Below it, `evaling run` exits non-zero.",
    )
    min_score: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description="Minimum overall weighted score. Below it, `evaling run` exits non-zero.",
    )
    baseline: str | None = Field(
        default=None,
        description="Gate against a previous run rather than an absolute number. `regression` "
        "means the pinned baseline; a run id means that run.",
    )


class EvalConfig(StrictModel):
    settings: Settings = Field(
        default_factory=Settings,
        description="Machine and workspace concerns: where runs go, how many run at once.",
    )
    models: list[ModelSpec] = Field(
        min_length=1,
        description="The systems under test, plus any model used only for judging.",
    )
    variants: list[VariantSpec] = Field(
        min_length=1,
        description="The prompts being compared. At least one is required; comparing anything "
        "needs two.",
    )
    cases: CaseFileRef | CaseSourceRef | list[Case] = Field(
        description="The inputs to run against: a list of cases, {file: path} for a dataset, or "
        "{source: 'file.py:factory'} to page them in from your own code."
    )
    scorecard: list[CriterionSpec] = Field(
        min_length=1, description="Weighted criteria that grade every cell."
    )
    judges: dict[str, JudgeSpec] = Field(
        default_factory=dict,
        description="Named autoraters, referenced by name from an `llm-judge` scorer.",
    )
    thresholds: Thresholds = Field(
        default_factory=Thresholds,
        description="Gates that decide the exit code of `evaling run`.",
    )
    privacy: Privacy = Field(
        default_factory=Privacy,
        description="Controls for evaluating data that must not be readable afterwards.",
    )

    # Directory containing the config file; relative paths resolve against it.
    _base_dir: Path = PrivateAttr(default=Path())

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    @model_validator(mode="after")
    def _cross_references(self) -> "EvalConfig":
        _require_unique((m.id for m in self.models), "model id")
        _require_unique((v.name for v in self.variants), "variant name")
        # Duplicates silently collapse in a record's scores, and in no-look
        # mode a name whitelisted for a Python scorer would then carry another
        # scorer's detail past redaction.
        _require_unique((c.criterion for c in self.scorecard), "criterion")
        if isinstance(self.cases, list):
            if not self.cases:
                raise ValueError("cases: at least one case is required")
            _require_unique((c.id for c in self.cases if c.id is not None), "case id")

        model_ids = {m.id for m in self.models}
        judged: dict[str, str] = {}
        for name, judge in self.judges.items():
            if judge.model not in model_ids:
                raise ValueError(f"judge {name!r} references unknown model {judge.model!r}")
            judged.setdefault(judge.model, name)

        by_id = {model.id: model for model in self.models}
        for model_id, judge_name in judged.items():
            if by_id[model_id].role == "candidate":
                raise ValueError(
                    f"model {model_id!r} is used by judge {judge_name!r}, so its role must be "
                    f"explicit. Add one of:\n"
                    f"  role: judge   - called by judges, never evaluated\n"
                    f"  role: both    - evaluated as a candidate and used as a judge"
                )
        if not any(model.role in ("candidate", "both") for model in self.models):
            raise ValueError(
                "no model is a candidate, so there is nothing to evaluate. Give at least "
                "one model role: candidate (the default) or role: both."
            )
        for model in self.models:
            if model.role == "both" and model.id not in judged:
                raise ValueError(
                    f"model {model.id!r} has role 'both' but no judge uses it. Use "
                    f"role: candidate if you only want it evaluated."
                )
            if model.role == "judge" and model.id not in judged:
                raise ValueError(
                    f"model {model.id!r} has role 'judge' but no judge uses it, so it would "
                    f"never be called. Reference it from a judge, or give it "
                    f"role: candidate to evaluate it."
                )

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
