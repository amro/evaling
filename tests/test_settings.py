from pathlib import Path

import pytest

from evaling.config import ConfigError, Settings
from evaling.config.settings import default_user_config_path, resolve_settings

MISSING = Path("/nonexistent/evaling-config.yaml")


def resolve(cli=None, eval_settings=None, env=None, user_config_path=MISSING, base_dir=None):
    return resolve_settings(
        cli, eval_settings, env=env or {}, user_config_path=user_config_path, base_dir=base_dir
    )


def test_defaults_when_no_layers_present():
    settings = resolve()
    assert settings == Settings()
    assert settings.output_dir == Path(".evaling/runs")
    assert settings.cache_dir == Path(".evaling/cache")
    assert settings.concurrency == 8
    assert settings.cache is True


def test_user_config_overrides_defaults(tmp_path):
    user = tmp_path / "config.yaml"
    user.write_text("concurrency: 2\n", encoding="utf-8")
    settings = resolve(user_config_path=user)
    assert settings.concurrency == 2
    assert settings.cache is True  # untouched fields keep defaults


def test_eval_config_settings_override_user_config(tmp_path):
    user = tmp_path / "config.yaml"
    user.write_text("concurrency: 2\noutput_dir: /from-user\n", encoding="utf-8")
    eval_settings = Settings.model_validate({"concurrency": 4})
    settings = resolve(eval_settings=eval_settings, user_config_path=user)
    assert settings.concurrency == 4  # eval config wins
    assert settings.output_dir == Path("/from-user")  # not set in eval config


def test_eval_settings_only_override_explicitly_set_fields(tmp_path):
    user = tmp_path / "config.yaml"
    user.write_text("cache: false\n", encoding="utf-8")
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
    user.write_text("concurency: 2\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="concurency"):
        resolve(user_config_path=user)


def test_user_config_invalid_yaml_rejected(tmp_path):
    user = tmp_path / "config.yaml"
    user.write_text("cache: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        resolve(user_config_path=user)


def test_empty_user_config_ignored(tmp_path):
    user = tmp_path / "config.yaml"
    user.write_text("", encoding="utf-8")
    assert resolve(user_config_path=user) == Settings()


def test_default_user_config_path_is_under_home():
    assert default_user_config_path().is_absolute()
    assert default_user_config_path().name == "config.yaml"


def test_out_of_range_env_value_is_config_error():
    # Regression: EVALING_CONCURRENCY=0 leaked a raw pydantic ValidationError
    # (traceback, exit 1) instead of a clean ConfigError (exit 2).
    with pytest.raises(ConfigError, match="invalid settings: concurrency"):
        resolve(env={"EVALING_CONCURRENCY": "0"})


def test_out_of_range_cli_value_is_config_error():
    with pytest.raises(ConfigError, match="invalid settings: concurrency"):
        resolve(cli={"concurrency": -3})


def test_user_config_path_from_env(tmp_path):
    user = tmp_path / "custom.yaml"
    user.write_text("concurrency: 3\n", encoding="utf-8")
    settings = resolve_settings(env={"EVALING_USER_CONFIG": str(user)}, user_config_path=None)
    assert settings.concurrency == 3


class TestRelativeDirectoriesAnchorToTheConfig:
    """Where a relative `output_dir` points depends on which layer set it.

    Before this, everything resolved against the working directory, so
    `evaling -c a/eval.yaml run` wrote to ./.evaling/runs while
    `cd a && evaling list` read a/.evaling/runs and reported nothing. Every
    other relative path in a config already resolved against the config's own
    directory; this was the exception.
    """

    def test_the_default_lands_beside_the_config(self, tmp_path):
        settings = resolve(base_dir=tmp_path)
        assert settings.output_dir == tmp_path / ".evaling/runs"
        assert settings.cache_dir == tmp_path / ".evaling/cache"

    def test_a_relative_path_in_the_config_lands_beside_it(self, tmp_path):
        eval_settings = Settings.model_validate({"output_dir": "runs"})
        settings = resolve(eval_settings=eval_settings, base_dir=tmp_path)
        assert settings.output_dir == tmp_path / "runs"

    def test_an_absolute_path_in_the_config_is_left_alone(self, tmp_path):
        # A real absolute path, not "/somewhere/else": on Windows that is
        # rooted but not absolute, so it would legitimately be anchored and
        # this test would fail there for the wrong reason.
        elsewhere = (tmp_path / "elsewhere").resolve()
        eval_settings = Settings.model_validate({"output_dir": str(elsewhere)})
        settings = resolve(eval_settings=eval_settings, base_dir=tmp_path / "project")
        assert settings.output_dir == elsewhere

    def test_a_relative_path_in_the_user_config_lands_beside_the_eval_config(self, tmp_path):
        """One rule for every file: relative means relative to the project."""
        user = tmp_path / "user.yaml"
        user.write_text("output_dir: shared-runs\n", encoding="utf-8")
        settings = resolve(user_config_path=user, base_dir=tmp_path / "project")
        assert settings.output_dir == tmp_path / "project" / "shared-runs"

    def test_a_cli_flag_stays_relative_to_the_working_directory(self, tmp_path):
        """Typed in the moment, so it means what it says where you're standing."""
        settings = resolve({"output_dir": Path("here")}, base_dir=tmp_path)
        assert settings.output_dir == Path("here")

    def test_an_env_var_stays_relative_to_the_working_directory(self, tmp_path):
        settings = resolve(env={"EVALING_OUTPUT_DIR": "here"}, base_dir=tmp_path)
        assert settings.output_dir == Path("here")

    def test_without_a_config_nothing_is_anchored(self):
        assert resolve().output_dir == Path(".evaling/runs")
