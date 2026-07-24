from pathlib import Path

import pytest

from evaling.config import CaseFileRef, ConfigError, load_config

FIXTURES = Path(__file__).parent / "fixtures"


def test_loads_requirements_appendix_example():
    cfg = load_config(FIXTURES / "example_eval.yaml")
    assert [m.id for m in cfg.models] == ["claude-sonnet-5", "local-llama"]
    assert [v.name for v in cfg.variants] == ["concise", "detailed"]
    assert isinstance(cfg.cases, CaseFileRef)
    assert cfg.scorecard[0].scorer.params["judge"] == "quality-judge"
    assert cfg.judges["quality-judge"].model == "claude-sonnet-5"
    assert cfg.thresholds.min_pass_rate == 0.9
    assert cfg.thresholds.baseline == "regression"


def test_base_dir_is_config_directory():
    cfg = load_config(FIXTURES / "example_eval.yaml")
    assert cfg.base_dir == FIXTURES.resolve()


def test_missing_file_raises_config_error():
    with pytest.raises(ConfigError, match="not found"):
        load_config(FIXTURES / "does_not_exist.yaml")


def test_invalid_yaml_syntax_names_file(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("models: [unclosed\n  - nope: {")
    with pytest.raises(ConfigError, match=r"bad\.yaml.*invalid YAML") as exc_info:
        load_config(bad)
    assert "Traceback" not in str(exc_info.value)


def test_non_mapping_top_level_rejected(tmp_path):
    bad = tmp_path / "list.yaml"
    bad.write_text("- just\n- a\n- list\n")
    with pytest.raises(ConfigError, match="top level must be a mapping, got list"):
        load_config(bad)


def test_validation_error_message_is_readable(tmp_path):
    bad = tmp_path / "eval.yaml"
    bad.write_text(
        """
models:
  - id: m1
    provider: nonsense
variants:
  - name: v1
    prompt: p.yaml
cases:
  - vars: {}
scorecard:
  - criterion: acc
    scorer: {type: exact}
"""
    )
    with pytest.raises(ConfigError) as exc_info:
        load_config(bad)
    message = str(exc_info.value)
    assert "eval.yaml: invalid config (1 error):" in message
    assert "models.0.provider" in message


def test_validation_error_reports_multiple_errors(tmp_path):
    bad = tmp_path / "eval.yaml"
    bad.write_text(
        """
models:
  - id: m1
    provider: nonsense
variants: []
cases:
  - vars: {}
scorecard:
  - criterion: acc
    scorer: {type: exact}
"""
    )
    with pytest.raises(ConfigError, match=r"invalid config \(2 errors\):"):
        load_config(bad)
