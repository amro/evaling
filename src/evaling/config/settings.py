"""Layered resolution of workspace settings.

Precedence, most specific wins:

1. CLI flags
2. Environment variables (EVALING_*)
3. ``settings:`` block in the eval config
4. User config at ~/.config/evaling/config.yaml
5. Built-in defaults
"""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from evaling.config.errors import ConfigError
from evaling.config.loader import _format_validation_error
from evaling.config.schema import Settings

ENV_VARS = {
    "EVALING_OUTPUT_DIR": "output_dir",
    "EVALING_CACHE_DIR": "cache_dir",
    "EVALING_CONCURRENCY": "concurrency",
    "EVALING_CACHE": "cache",
}

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
) -> Settings:
    """Merge all settings layers into a final Settings value.

    ``cli`` maps Settings field names to values; None values mean "flag not
    given" and are ignored. Only fields explicitly set in the eval config or
    user config override lower layers.
    """
    values: dict[str, Any] = {}

    user = _load_user_config(user_config_path or default_user_config_path())
    if user is not None:
        values.update({name: getattr(user, name) for name in user.model_fields_set})

    if eval_settings is not None:
        explicit = eval_settings.model_fields_set
        values.update({name: getattr(eval_settings, name) for name in explicit})

    values.update(_from_env(os.environ if env is None else env))

    for name, value in (cli or {}).items():
        if name not in Settings.model_fields:
            raise ValueError(f"unknown setting: {name!r}")
        if value is not None:
            values[name] = value

    return Settings(**values)


def _load_user_config(path: Path) -> Settings | None:
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
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
