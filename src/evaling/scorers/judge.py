"""The llm-judge scorer: an autorater grades outputs against a rubric.

A judge is a first-class prompt: rubric messages (text-only) + a judge model.
The rubric templates receive ``output``, ``expected``, and ``vars`` (the
case's variables). The judge must answer with JSON containing ``score`` and
optionally ``passed`` and ``rationale``; markdown fences are tolerated.

Params: ``judge`` (name, resolved by the factory), ``scale`` (what a maximal
score reads as, default 1 — set 5 for a 1-5 rubric), ``pass_at`` (normalized
score needed to pass when the judge omits ``passed``, default 0.5).
"""

import json
import math
from numbers import Real

from evaling.config.schema import Case, JudgeSpec, Message, ModelSpec, TextPart
from evaling.providers.base import CompletionRequest, Provider
from evaling.providers.retry import call_with_retries
from evaling.render import RenderedMessage, RenderedText
from evaling.scorers.base import Scorer, ScoreResult, ScoringError, parse_json_lenient
from evaling.templating import render_text


def _number(params, name, default, judge_name):
    """A numeric scorer parameter, or a message rather than a traceback.

    ScorerSpec allows extra keys, so the schema accepts any value here and a
    bare ValueError from float() is not something the CLI catches.
    """
    raw = params.get(name, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ScoringError(f"judge {judge_name!r}: {name} must be a number, got {raw!r}") from None
    if not math.isfinite(value):
        raise ScoringError(f"judge {judge_name!r}: {name} must be finite, got {raw!r}")
    return value


class JudgeScorer(Scorer):
    def __init__(
        self,
        params,
        base_dir,
        *,
        judge_name: str,
        judge: JudgeSpec,
        rubric: list[Message],
        model: ModelSpec,
        provider: Provider,
        call=None,
    ):
        super().__init__(params, base_dir)
        self.judge_name = judge_name
        self.model = model
        self.provider = provider
        #: The engine's governed call, when running inside a run: it applies the
        #: cost budget and this model's concurrency and rate limits. A judge is
        #: a billable call like any other, so calling the provider directly puts
        #: real spend outside --max-cost.
        self.call = call
        self.rubric = rubric
        self.scale = _number(params, "scale", 1.0, self.judge_name)
        if self.scale <= 0:
            raise ScoringError(f"judge {judge_name!r}: 'scale' must be positive, got {self.scale}")
        self.pass_at = _number(params, "pass_at", 0.5, self.judge_name)
        if not 0.0 <= self.pass_at <= 1.0:
            raise ScoringError(
                f"judge {judge_name!r}: 'pass_at' must be in [0, 1], got {self.pass_at}"
            )
        for message in rubric:
            content = message.content
            if not isinstance(content, str) and any(
                not isinstance(part, TextPart) for part in content
            ):
                raise ScoringError(f"judge {judge_name!r}: rubrics support text content only")

    async def score(self, output: str, case: Case) -> ScoreResult:
        context = {"output": output, "expected": case.expected, "vars": dict(case.vars)}
        rendered = []
        for index, message in enumerate(self.rubric, start=1):
            where = f"judge {self.judge_name!r} rubric message {index}"
            content = message.content
            if isinstance(content, str):
                texts = [render_text(content, context, where)]
            else:
                texts = [render_text(part.text, context, where) for part in content]
            rendered.append(
                RenderedMessage(
                    role=message.role, parts=tuple(RenderedText(text) for text in texts)
                )
            )

        if self.call is not None:
            completion = await self.call(self.model, rendered)
        else:
            # Standalone use (a scorer built outside a run): no budget to
            # respect, so call the provider directly with the same retry policy.
            request = CompletionRequest(model=self.model, messages=rendered)
            retry_kwargs = (
                {}
                if self.model.max_retries is None
                else {"max_attempts": self.model.max_retries + 1}
            )
            completion = await call_with_retries(
                lambda: self.provider.complete(request), **retry_kwargs
            )
        return self._parse_verdict(completion.text)

    def _parse_verdict(self, text: str) -> ScoreResult:
        try:
            data = parse_json_lenient(text)
        except json.JSONDecodeError as exc:
            raise ScoringError(
                f"judge {self.judge_name!r} did not return JSON: {exc.msg}: {text[:120]!r}"
            ) from exc
        score = data.get("score") if isinstance(data, dict) else None
        # `bool` passes isinstance(..., Real), so a judge answering `true`
        # scored 1.0; NaN survived the clamp because every comparison with it
        # is False. Both inflated a result instead of failing the criterion.
        if isinstance(score, bool) or not isinstance(score, Real) or not math.isfinite(score):
            raise ScoringError(
                f"judge {self.judge_name!r} verdict must be JSON with a finite numeric "
                f"'score', got {text[:120]!r}"
            )
        normalized = max(0.0, min(1.0, float(score) / self.scale))
        passed = data.get("passed")
        if not isinstance(passed, bool):
            passed = normalized >= self.pass_at
        return ScoreResult(normalized, passed, data.get("rationale"))
