import asyncio
import json

import pytest

from evaling.config import Case, EvalConfig
from evaling.providers import create_provider
from evaling.scorers import create_scorer, create_scorers
from evaling.scorers.base import ScoringError
from evaling.scorers.judge import JudgeScorer


def judge_config(response, rubric=None, scorer_params=None, tmp_path=None):
    """Config whose judge model is a mock returning a fixed verdict."""
    cfg = EvalConfig.model_validate(
        {
            "models": [
                {"id": "main", "provider": "mock"},
                {
                    "id": "judge-model",
                    "provider": "mock",
                    "role": "judge",
                    "params": {"response": response},
                },
            ],
            "variants": [{"name": "v1", "prompt": [{"role": "user", "content": "{{ q }}"}]}],
            "cases": [{"vars": {"q": "hi"}}],
            "scorecard": [
                {
                    "criterion": "quality",
                    "scorer": {"type": "llm-judge", "judge": "grader", **(scorer_params or {})},
                }
            ],
            "judges": {
                "grader": {
                    "model": "judge-model",
                    "rubric": rubric
                    or [
                        {"role": "system", "content": "Grade 0-1. Answer JSON."},
                        {"role": "user", "content": "Got {{ output }}, want {{ expected }}"},
                    ],
                }
            },
        }
    )
    if tmp_path is not None:
        cfg._base_dir = tmp_path
    return cfg


def build_judge(config):
    providers = {m.id: create_provider(m) for m in config.models}
    return create_scorer(config.scorecard[0].scorer, config, providers)


def run_score(scorer, output="the answer", case=None):
    return asyncio.run(scorer.score(output, case or Case(expected="the answer")))


def test_judge_scores_from_json_verdict():
    verdict = json.dumps({"score": 0.9, "rationale": "close match"})
    result = run_score(build_judge(judge_config(verdict)))
    assert result.score == 0.9
    assert result.passed  # 0.9 >= default pass_at 0.5
    assert result.detail == "close match"


def test_judge_explicit_passed_wins():
    verdict = json.dumps({"score": 0.9, "passed": False})
    assert not run_score(build_judge(judge_config(verdict))).passed


def test_judge_scale_normalization():
    verdict = json.dumps({"score": 4})
    scorer = build_judge(judge_config(verdict, scorer_params={"scale": 5}))
    result = run_score(scorer)
    assert result.score == 0.8


def test_judge_score_clamped():
    verdict = json.dumps({"score": 9})
    assert run_score(build_judge(judge_config(verdict))).score == 1.0


def test_judge_non_json_raises():
    with pytest.raises(ScoringError, match="did not return JSON"):
        run_score(build_judge(judge_config("It looks great!")))


def test_judge_missing_score_raises():
    with pytest.raises(ScoringError, match="numeric 'score'"):
        run_score(build_judge(judge_config('{"rating": 5}')))


def test_rubric_receives_output_expected_and_vars():
    # mock judge echoes its last user message: prove the rubric rendered
    config = judge_config(
        response=None,
        rubric=[{"role": "user", "content": "O={{ output }} E={{ expected }} Q={{ vars.q }}"}],
    )
    # remove fixed response so the judge echoes
    config.models[1].params.pop("response")
    scorer = build_judge(config)
    with pytest.raises(ScoringError, match=r"O=out E=exp Q=hi"):
        asyncio.run(scorer.score("out", Case(expected="exp", vars={"q": "hi"})))


def test_judge_verdict_with_preamble_parses():
    verdict = 'Here is my assessment:\n```json\n{"score": 0.9}\n```\nLet me know!'
    assert run_score(build_judge(judge_config(verdict))).score == 0.9


def test_invalid_scale_rejected_at_construction():
    with pytest.raises(ScoringError, match="'scale' must be positive"):
        build_judge(judge_config('{"score": 1}', scorer_params={"scale": 0}))
    with pytest.raises(ScoringError, match="'scale' must be positive"):
        build_judge(judge_config('{"score": 1}', scorer_params={"scale": -5}))


def test_invalid_pass_at_rejected_at_construction():
    with pytest.raises(ScoringError, match=r"'pass_at' must be in \[0, 1\]"):
        build_judge(judge_config('{"score": 1}', scorer_params={"pass_at": 1.5}))


@pytest.mark.parametrize("from_file", [False, True])
@pytest.mark.parametrize("media", [{"image": "x.png"}, {"audio": "x.wav"}])
def test_media_rubric_rejected_at_construction(tmp_path, from_file, media):
    rubric = [{"role": "user", "content": [{"text": "grade"}, media]}]
    if from_file:
        path = tmp_path / "rubric.yaml"
        path.write_text(json.dumps(rubric), encoding="utf-8")
        rubric = "rubric.yaml"
    config = judge_config(
        '{"score": 1}',
        rubric=rubric,
        tmp_path=tmp_path,
    )
    with pytest.raises(ScoringError, match="text content only"):
        build_judge(config)


def test_create_scorers_builds_full_scorecard():
    config = judge_config('{"score": 1}')
    providers = {m.id: create_provider(m) for m in config.models}
    scorers = create_scorers(config, providers)
    assert len(scorers) == 1
    criterion, scorer = scorers[0]
    assert criterion.criterion == "quality"
    assert isinstance(scorer, JudgeScorer)


class TestAJudgeCannotInflateItsScore:
    """A nonsense verdict must fail the criterion, not ace it.

    `bool` passes `isinstance(_, Real)`, so a judge answering `true` scored a
    perfect 1.0; NaN survived `max(0, min(1, x))` because every comparison
    with NaN is False. Both turned a broken judge into a passing result.
    """

    @pytest.mark.parametrize("verdict", ['{"score": true}', '{"score": false}'])
    def test_a_boolean_score_is_refused(self, verdict):
        with pytest.raises(ScoringError, match="finite numeric"):
            run_score(build_judge(judge_config(verdict)))

    @pytest.mark.parametrize("verdict", ['{"score": NaN}', '{"score": Infinity}'])
    def test_a_non_finite_score_is_refused(self, verdict):
        with pytest.raises(ScoringError, match="finite numeric"):
            run_score(build_judge(judge_config(verdict)))

    @pytest.mark.parametrize("value", ["wide", None, [1]])
    def test_a_non_numeric_scale_is_a_message(self, value):
        """ScorerSpec allows extra keys, so the schema accepts anything here."""
        with pytest.raises(ScoringError, match="must be a number"):
            build_judge(judge_config('{"score": 1}', scorer_params={"scale": value}))
