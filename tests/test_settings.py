from pathlib import Path

import pytest

from evaling.config import ConfigError, Settings
from evaling.config.settings import default_user_config_path, resolve_settings

MISSING = Path("/nonexistent/evaling-config.yaml")


def resolve(cli=None, eval_settings=None, env=None, user_config_path=MISSING):
    return resolve_settings(cli, eval_settings, env=env or {}, user_config_path=user_config_path)


def test_defaults_when_no_layers_present():
    settings = resolve()
    assert settings == Settings()
    assert settings.output_dir == Path(".evaling/runs")
    assert settings.cache_dir == Path(".evaling/cache")
    assert settings.concurrency == 8
    assert settings.cache is True


def test_user_config_overrides_defaults(tmp_path):
    user = tmp_path / "config.yaml"
    user.write_text("concurrency: 2\n")
    settings = resolve(user_config_path=user)
    assert settings.concurrency == 2
    assert settings.cache is True  # untouched fields keep defaults


def test_eval_config_settings_override_user_config(tmp_path):
    user = tmp_path / "config.yaml"
    user.write_text("concurrency: 2\noutput_dir: /from-user\n")
    eval_settings = Settings.model_validate({"concurrency": 4})
    settings = resolve(eval_settings=eval_settings, user_config_path=user)
    assert settings.concurrency == 4  # eval config wins
    assert settings.output_dir == Path("/from-user")  # not set in eval config


def test_eval_settings_only_override_explicitly_set_fields(tmp_path):
    user = tmp_path / "config.yaml"
    user.write_text("cache: false\n")
    # eval config sets nothing; its defaults must not clobber the user config
    settings = resolve(eval_settings=Settings(), user_config_path=user)
    assert settings.cache is False


def test_env_overrides_eval_config():
    eval_settings = Settings.model_validate({"concurrency": 4})
    settings = resolve(eval_settings=eval_settings, env={"EVALING_CONCURRENCY": "16"})
    assert settings.concurrency == 16


def test_cli_overrides_env():
    settings = resolve(
        cli={"concurrency": 3, "output_dir": Path("/cli")},
        env={"EVALING_CONCURRENCY": "16", "EVALING_OUTPUT_DIR": "/env"},
    )
    assert settings.concurrency == 3
    assert settings.output_dir == Path("/cli")


def test_cli_none_values_ignored():
    settings = resolve(cli={"concurrency": None}, env={"EVALING_CONCURRENCY": "16"})
    assert settings.concurrency == 16


def test_unknown_cli_setting_rejected():
    with pytest.raises(ValueError, match="unknown setting"):
        resolve(cli={"paralellism": 2})


def test_env_paths_and_bool():
    settings = resolve(
        env={
            "EVALING_OUTPUT_DIR": "/data/runs",
            "EVALING_CACHE_DIR": "/data/cache",
            "EVALING_CACHE": "off",
        }
    )
    assert settings.output_dir == Path("/data/runs")
    assert settings.cache_dir == Path("/data/cache")
    assert settings.cache is False


@pytest.mark.parametrize("raw,expected", [("1", True), ("TRUE", True), ("no", False), ("0", False)])
def test_env_bool_spellings(raw, expected):
    assert resolve(env={"EVALING_CACHE": raw}).cache is expected


def test_env_bad_int_raises_config_error():
    with pytest.raises(ConfigError, match="EVALING_CONCURRENCY must be an integer"):
        resolve(env={"EVALING_CONCURRENCY": "many"})


def test_env_bad_bool_raises_config_error():
    with pytest.raises(ConfigError, match="EVALING_CACHE must be a boolean"):
        resolve(env={"EVALING_CACHE": "maybe"})


def test_empty_env_value_ignored():
    assert resolve(env={"EVALING_CONCURRENCY": ""}).concurrency == 8


def test_user_config_with_unknown_key_rejected(tmp_path):
    user = tmp_path / "config.yaml"
    user.write_text("concurency: 2\n")
    with pytest.raises(ConfigError, match="concurency"):
        resolve(user_config_path=user)


def test_user_config_invalid_yaml_rejected(tmp_path):
    user = tmp_path / "config.yaml"
    user.write_text("cache: [unclosed")
    with pytest.raises(ConfigError, match="invalid YAML"):
        resolve(user_config_path=user)


def test_empty_user_config_ignored(tmp_path):
    user = tmp_path / "config.yaml"
    user.write_text("")
    assert resolve(user_config_path=user) == Settings()


def test_default_user_config_path_is_under_home():
    assert default_user_config_path().is_absolute()
    assert default_user_config_path().name == "config.yaml"
