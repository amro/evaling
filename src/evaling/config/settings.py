"""Layered resolution of workspace settings.

Precedence, most specific wins:

1. CLI flags
2. Environment variables (EVALING_*)
3. ``settings:`` block in the eval config
4. User config at ~/.config/evaling/config.yaml
5. Built-in defaults

Relative directories anchor to whichever of those set them. A path written in
a file resolves against the eval config's directory, like every other relative
path in a config; a path typed on the command line or exported in the
environment resolves against the working directory, because that is where the
person typing it is standing. See :func:`_anchor`.
"""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from evaling.config.errors import ConfigError
from evaling.config.loader import _format_validation_error
from evaling.config.schema import Settings
from evaling.textfile import read_yaml

ENV_VARS = {
    "EVALING_OUTPUT_DIR": "output_dir",
    "EVALING_CACHE_DIR": "cache_dir",
    "EVALING_CONCURRENCY": "concurrency",
    "EVALING_CACHE": "cache",
}

# Overrides where the user config file is looked up (not a Settings field).
USER_CONFIG_ENV_VAR = "EVALING_USER_CONFIG"

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def default_user_config_path() -> Path:
    return Path("~/.config/evaling/config.yaml").expanduser()


def resolve_settings(
    cli: Mapping[str, Any] | None = None,
    eval_settings: Settings | None = None,
    *,
    env: Mapping[str, str] | None = None,
    user_config_path: Path | None = None,
    base_dir: Path | None = None,
) -> Settings:
    """Merge all settings layers into a final Settings value.

    ``cli`` maps Settings field names to values; None values mean "flag not
    given" and are ignored. Only fields explicitly set in the eval config or
    user config override lower layers.

    ``base_dir`` is the directory of the eval config in play. Given one, the
    file-supplied and default directories are resolved against it rather than
    against the process's working directory.
    """
    values: dict[str, Any] = {}

    env_map = os.environ if env is None else env
    if user_config_path is None:
        env_user_config = env_map.get(USER_CONFIG_ENV_VAR)
        user_config_path = Path(env_user_config) if env_user_config else default_user_config_path()
    user = _load_user_config(user_config_path)
    if user is not None:
        values.update({name: getattr(user, name) for name in user.model_fields_set})

    if eval_settings is not None:
        explicit = eval_settings.model_fields_set
        values.update({name: getattr(eval_settings, name) for name in explicit})

    # Before env and CLI, which are deliberately left alone.
    _anchor(values, base_dir)

    values.update(_from_env(os.environ if env is None else env))

    for name, value in (cli or {}).items():
        if name not in Settings.model_fields:
            raise ValueError(f"unknown setting: {name!r}")
        if value is not None:
            values[name] = value

    try:
        return Settings(**values)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(part) for part in first["loc"]) or "<settings>"
        raise ConfigError(f"invalid settings: {loc}: {first['msg']}") from exc


#: Settings whose values are directories, and so can be relative to something.
PATH_FIELDS = ("output_dir", "cache_dir")


def _anchor(values: dict[str, Any], base_dir: Path | None) -> None:
    """Resolve file-supplied and default directories against the config's own.

    A project's runs and cache belong beside its config, not beside whatever
    directory you happened to be in. Without this, `evaling -c a/eval.yaml run`
    wrote to ./.evaling/runs while `cd a && evaling list` read a/.evaling/runs,
    so the second command reported no runs at all -- and every other relative
    path in a config (prompts, datasets, attachments) already resolved against
    the config's directory, so this was the one exception.

    Defaults are anchored too: a config that says nothing about `output_dir`
    still belongs to a project. Values from the environment or the command line
    are not, since those are typed relative to where you are standing.
    """
    if base_dir is None:
        return
    for name in PATH_FIELDS:
        raw = values.get(name, Settings.model_fields[name].default)
        path = Path(raw)
        if not path.is_absolute():
            values[name] = Path(base_dir) / path


def _load_user_config(path: Path) -> Settings | None:
    if not path.exists():
        return None
    data = read_yaml(path, ConfigError, missing=f"user config not found: {path}")
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping, got {type(data).__name__}")
    try:
        return Settings.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(path, exc)) from exc


def _from_env(env: Mapping[str, str]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for var, field in ENV_VARS.items():
        raw = env.get(var)
        if raw is None or raw == "":
            continue
        if field in ("output_dir", "cache_dir"):
            values[field] = Path(raw)
        elif field == "concurrency":
            try:
                values[field] = int(raw)
            except ValueError:
                raise ConfigError(f"{var} must be an integer, got {raw!r}") from None
        elif field == "cache":
            values[field] = _parse_bool(var, raw)
    return values


def _parse_bool(var: str, raw: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in _TRUTHY:
        return True
    if lowered in _FALSY:
        return False
    raise ConfigError(f"{var} must be a boolean (true/false/1/0/yes/no/on/off), got {raw!r}")
