from pathlib import Path

import pytest
from pydantic import ValidationError

from evaling.config.schema import (
    Case,
    CaseFileRef,
    EvalConfig,
    Message,
    ModelSpec,
    Settings,
)


def minimal_config(**overrides):
    data = {
        "models": [{"id": "m1", "provider": "mock"}],
        "variants": [{"name": "v1", "prompt": "prompts/v1.yaml"}],
        "cases": [{"vars": {"q": "hi"}}],
        "scorecard": [{"criterion": "acc", "scorer": {"type": "exact"}}],
    }
    data.update(overrides)
    return data


def test_minimal_config_valid_with_defaults():
    cfg = EvalConfig.model_validate(minimal_config())
    assert cfg.settings == Settings()
    assert cfg.settings.cache is True
    assert cfg.settings.concurrency == 8
    assert cfg.settings.output_dir == Path(".evaling/runs")
    assert cfg.judges == {}
    assert cfg.thresholds.baseline is None
    assert cfg.scorecard[0].weight == 1.0


def test_unknown_top_level_key_rejected():
    with pytest.raises(ValidationError, match="modles"):
        EvalConfig.model_validate(minimal_config(modles=[]))


def test_unknown_nested_key_rejected():
    bad = minimal_config(cases=[{"vars": {}, "expcted": "typo"}])
    with pytest.raises(ValidationError, match="expcted"):
        EvalConfig.model_validate(bad)


def test_empty_models_rejected():
    with pytest.raises(ValidationError):
        EvalConfig.model_validate(minimal_config(models=[]))


def test_empty_cases_list_rejected():
    with pytest.raises(ValidationError, match="at least one case"):
        EvalConfig.model_validate(minimal_config(cases=[]))


def test_cases_as_file_reference():
    cfg = EvalConfig.model_validate(minimal_config(cases={"file": "cases.jsonl"}))
    assert isinstance(cfg.cases, CaseFileRef)
    assert cfg.cases.file == "cases.jsonl"


def test_duplicate_model_ids_rejected():
    models = [{"id": "m1", "provider": "mock"}, {"id": "m1", "provider": "mock"}]
    with pytest.raises(ValidationError, match="duplicate model id"):
        EvalConfig.model_validate(minimal_config(models=models))


def test_duplicate_variant_names_rejected():
    variants = [{"name": "v", "prompt": "a"}, {"name": "v", "prompt": "b"}]
    with pytest.raises(ValidationError, match="duplicate variant name"):
        EvalConfig.model_validate(minimal_config(variants=variants))


def test_duplicate_case_ids_rejected():
    cases = [{"id": "c1"}, {"id": "c1"}]
    with pytest.raises(ValidationError, match="duplicate case id"):
        EvalConfig.model_validate(minimal_config(cases=cases))


def test_case_ids_may_be_omitted():
    cfg = EvalConfig.model_validate(minimal_config(cases=[{}, {}]))
    assert [c.id for c in cfg.cases] == [None, None]


def test_unknown_provider_rejected():
    with pytest.raises(ValidationError):
        ModelSpec.model_validate({"id": "m", "provider": "gemini"})


def test_openai_compatible_requires_base_url():
    with pytest.raises(ValidationError, match="base_url"):
        ModelSpec.model_validate({"id": "m", "provider": "openai-compatible"})


def test_command_provider_requires_command():
    with pytest.raises(ValidationError, match="command"):
        ModelSpec.model_validate({"id": "m", "provider": "command"})


@pytest.mark.parametrize(
    "pricing",
    [
        {"input": -1, "output": 5},
        {"input": 5, "output": -1},
        {"input": "cheap", "output": 5},
        {"input": 5},
        {"input": True, "output": 5},
        "free",
        # NaN clears `value < 0` — every comparison against it is False — and
        # inf clears it honestly. Both are one YAML token (`.nan`, `.inf`)
        # away from a number, and both used to load, then produce a NaN cost
        # that failed the cell *after* the API call was billed.
        {"input": float("nan"), "output": 5},
        {"input": 5, "output": float("nan")},
        {"input": float("inf"), "output": 5},
        {"input": 5, "output": float("inf")},
    ],
)
def test_malformed_pricing_rejected_at_load(pricing):
    # Fails before any spend: a negative rate would shrink tracked spend and
    # defeat --max-cost.
    with pytest.raises(ValidationError, match="pricing"):
        ModelSpec.model_validate({"id": "m", "provider": "mock", "params": {"pricing": pricing}})


def test_valid_pricing_accepted():
    spec = ModelSpec.model_validate(
        {"id": "m", "provider": "mock", "params": {"pricing": {"input": 1.25, "output": 10}}}
    )
    assert spec.params["pricing"]["output"] == 10


def test_command_only_valid_for_command_provider():
    with pytest.raises(ValidationError, match="only valid"):
        ModelSpec.model_validate({"id": "m", "provider": "mock", "command": "echo"})


def test_inline_prompt_messages_parse():
    variant = {
        "name": "v1",
        "prompt": [
            {"role": "system", "content": "Be brief."},
            {
                "role": "user",
                "content": [{"text": "{{ q }}"}, {"image": "{{ files.photo }}"}],
            },
        ],
    }
    cfg = EvalConfig.model_validate(minimal_config(variants=[variant]))
    messages = cfg.variants[0].prompt
    assert messages[0].content == "Be brief."
    assert messages[1].content[1].image == "{{ files.photo }}"


def test_all_content_part_types_parse():
    msg = Message.model_validate(
        {
            "role": "user",
            "content": [
                {"text": "t"},
                {"image": "i.png"},
                {"file": "d.pdf"},
                {"audio": "a.mp3"},
                {"video": "v.mp4"},
            ],
        }
    )
    assert len(msg.content) == 5


def test_invalid_content_part_rejected():
    with pytest.raises(ValidationError):
        Message.model_validate({"role": "user", "content": [{"hologram": "h.holo"}]})


def test_invalid_role_rejected():
    with pytest.raises(ValidationError):
        Message.model_validate({"role": "tool", "content": "x"})


def test_scorer_extra_params_allowed_and_exposed():
    cfg = EvalConfig.model_validate(
        minimal_config(
            scorecard=[
                {
                    "criterion": "fmt",
                    "weight": 2,
                    "scorer": {"type": "regex", "pattern": "^ok$"},
                }
            ]
        )
    )
    assert cfg.scorecard[0].scorer.params == {"pattern": "^ok$"}


def test_unknown_scorer_type_rejected():
    bad = minimal_config(scorecard=[{"criterion": "x", "scorer": {"type": "vibes"}}])
    with pytest.raises(ValidationError):
        EvalConfig.model_validate(bad)


def test_zero_weight_rejected():
    bad = minimal_config(scorecard=[{"criterion": "x", "weight": 0, "scorer": {"type": "exact"}}])
    with pytest.raises(ValidationError):
        EvalConfig.model_validate(bad)


@pytest.mark.parametrize("weight", [float("inf"), float("nan")])
def test_a_non_finite_weight_is_rejected(weight):
    """`gt=0` is true of inf, and a weighted score becomes inf/inf — NaN.

    That NaN then spreads: into the cell score, the aggregates, the gate
    comparison, and a `--json` export where it is not valid JSON at all.
    """
    bad = minimal_config(
        scorecard=[{"criterion": "x", "weight": weight, "scorer": {"type": "exact"}}]
    )
    with pytest.raises(ValidationError):
        EvalConfig.model_validate(bad)


@pytest.mark.parametrize("timeout", [float("inf"), float("nan")])
def test_a_non_finite_timeout_is_rejected(timeout):
    """An infinite timeout is a request that never gives up."""
    with pytest.raises(ValidationError):
        ModelSpec.model_validate({"id": "m", "provider": "mock", "timeout_s": timeout})


@pytest.mark.parametrize(
    "value", ["sk-ant-api03-looks-like-a-real-key", "sk-proj-abc123", "has space", ""]
)
def test_api_key_env_must_look_like_a_variable_name(value):
    """It names a variable; a value here is a credential in a committed file.

    The config is serialized verbatim into every run's snapshot, so a key
    pasted here spreads from the repo into the run directory.
    """
    with pytest.raises(ValidationError):
        ModelSpec.model_validate({"id": "m", "provider": "mock", "api_key_env": value})


@pytest.mark.parametrize("value", ["ANTHROPIC_API_KEY", "my_key", "_X1"])
def test_real_variable_names_are_accepted(value):
    assert (
        ModelSpec.model_validate({"id": "m", "provider": "mock", "api_key_env": value}).api_key_env
        == value
    )


def test_llm_judge_requires_judge_name():
    bad = minimal_config(scorecard=[{"criterion": "q", "scorer": {"type": "llm-judge"}}])
    with pytest.raises(ValidationError, match="requires a 'judge' name"):
        EvalConfig.model_validate(bad)


def test_llm_judge_unknown_judge_rejected():
    bad = minimal_config(
        scorecard=[{"criterion": "q", "scorer": {"type": "llm-judge", "judge": "nope"}}]
    )
    with pytest.raises(ValidationError, match="unknown judge"):
        EvalConfig.model_validate(bad)


def test_llm_judge_with_defined_judge_valid():
    cfg = EvalConfig.model_validate(
        minimal_config(
            models=[{"id": "m1", "provider": "mock", "role": "both"}],
            scorecard=[{"criterion": "q", "scorer": {"type": "llm-judge", "judge": "j"}}],
            judges={"j": {"model": "m1", "rubric": "prompts/rubric.yaml"}},
        )
    )
    assert cfg.judges["j"].model == "m1"


class TestModelRole:
    """A judge's model is not a system under test unless you say so."""

    def config(self, **model_overrides):
        return minimal_config(
            models=[
                {"id": "m1", "provider": "mock"},
                {"id": "j", "provider": "mock", **model_overrides},
            ],
            scorecard=[{"criterion": "q", "scorer": {"type": "llm-judge", "judge": "j"}}],
            judges={"j": {"model": "j", "rubric": "prompts/rubric.yaml"}},
        )

    def test_default_is_candidate(self):
        cfg = EvalConfig.model_validate(minimal_config())
        assert cfg.models[0].role == "candidate"

    def test_a_judges_model_must_declare_its_role(self):
        """Leaving it at the default is the mistake this exists to catch."""
        with pytest.raises(ValidationError, match="its role must be explicit"):
            EvalConfig.model_validate(self.config())

    def test_the_error_names_both_ways_out(self):
        with pytest.raises(ValidationError) as caught:
            EvalConfig.model_validate(self.config())
        message = str(caught.value)
        assert "role: judge" in message and "role: both" in message

    @pytest.mark.parametrize("role", ["judge", "both"])
    def test_explicit_roles_are_accepted(self, role):
        assert EvalConfig.model_validate(self.config(role=role)) is not None

    def test_declaring_candidate_is_still_a_contradiction(self):
        with pytest.raises(ValidationError, match="its role must be explicit"):
            EvalConfig.model_validate(self.config(role="candidate"))

    def test_a_judge_role_nobody_uses_is_rejected(self):
        """Dead config: the model would never be called at all."""
        bad = minimal_config(
            models=[
                {"id": "m1", "provider": "mock"},
                {"id": "unused", "provider": "mock", "role": "judge"},
            ]
        )
        with pytest.raises(ValidationError, match="no judge uses it"):
            EvalConfig.model_validate(bad)

    def test_unknown_role_rejected(self):
        with pytest.raises(ValidationError):
            EvalConfig.model_validate(
                minimal_config(models=[{"id": "m1", "provider": "mock", "role": "referee"}])
            )


def test_judge_referencing_unknown_model_rejected():
    bad = minimal_config(judges={"j": {"model": "ghost", "rubric": "r.yaml"}})
    with pytest.raises(ValidationError, match="unknown model"):
        EvalConfig.model_validate(bad)


def test_threshold_bounds_enforced():
    with pytest.raises(ValidationError):
        EvalConfig.model_validate(minimal_config(thresholds={"min_pass_rate": 1.5}))
    with pytest.raises(ValidationError):
        # scores are normalized to [0,1]; a 0-100 assumption must fail loudly
        EvalConfig.model_validate(minimal_config(thresholds={"min_score": 80}))


def test_concurrency_must_be_positive():
    with pytest.raises(ValidationError):
        Settings.model_validate({"concurrency": 0})


def test_case_fields():
    case = Case.model_validate(
        {
            "id": "c1",
            "vars": {"q": "hi"},
            "files": {"photo": "./dog.jpg"},
            "expected": "answer",
            "human_label": 4,
        }
    )
    assert case.files["photo"] == "./dog.jpg"
    assert case.human_label == 4
