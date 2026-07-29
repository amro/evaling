"""Scorer registry: build scorer instances from scorecard specs."""

from evaling.config.loader import resolve_prompt
from evaling.config.schema import CriterionSpec, EvalConfig, ScorerSpec
from evaling.providers.base import Provider
from evaling.scorers.agreement import AgreementScorer
from evaling.scorers.base import Scorer, ScoreResult, ScoringError
from evaling.scorers.builtin import (
    ContainsScorer,
    ExactScorer,
    JsonSchemaScorer,
    JsonValidScorer,
    NotContainsScorer,
    RegexScorer,
)
from evaling.scorers.judge import JudgeScorer
from evaling.scorers.python_scorer import PythonScorer

__all__ = ["ScoreResult", "Scorer", "ScoringError", "create_scorer", "create_scorers"]

_SIMPLE = {
    "exact": ExactScorer,
    "contains": ContainsScorer,
    "not-contains": NotContainsScorer,
    "regex": RegexScorer,
    "json-valid": JsonValidScorer,
    "json-schema": JsonSchemaScorer,
    "python": PythonScorer,
    "agreement": AgreementScorer,
}


def create_scorer(
    spec: ScorerSpec,
    config: EvalConfig,
    providers: dict[str, Provider],
    call=None,
) -> Scorer:
    params = spec.params
    if spec.type in _SIMPLE:
        return _SIMPLE[spec.type](params, config.base_dir)
    if spec.type == "llm-judge":
        judge_name = params["judge"]  # validated by the config schema
        judge = config.judges[judge_name]
        model = next(m for m in config.models if m.id == judge.model)
        return JudgeScorer(
            params,
            config.base_dir,
            judge_name=judge_name,
            judge=judge,
            rubric=resolve_prompt(judge.rubric, config.base_dir),
            model=model,
            provider=providers[judge.model],
            call=call,
        )
    raise ScoringError(f"unknown scorer type {spec.type!r}")  # pragma: no cover


def create_scorers(
    config: EvalConfig, providers: dict[str, Provider], call=None
) -> list[tuple[CriterionSpec, Scorer]]:
    """Build one scorer per scorecard criterion, failing fast on bad config.

    ``call`` is the engine's governed model call. Judges use it so their
    requests pass through the cost budget and the judge model's own limits;
    without it they fall back to calling the provider directly.
    """
    return [
        (criterion, create_scorer(criterion.scorer, config, providers, call))
        for criterion in config.scorecard
    ]
