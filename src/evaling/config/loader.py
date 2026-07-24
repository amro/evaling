"""Load and validate eval.yaml files."""

from pathlib import Path

import yaml
from pydantic import ValidationError

from evaling.config.errors import ConfigError
from evaling.config.schema import EvalConfig


def load_config(path: str | Path) -> EvalConfig:
    """Load an eval config, raising ConfigError with a readable message on failure.

    The returned config's ``base_dir`` is the directory containing the file, for
    resolving relative paths (prompt files, case files, attachments).
    """
    path = Path(path)
    try:
        raw = path.read_text()
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except OSError as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping, got {type(data).__name__}")

    try:
        config = EvalConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(path, exc)) from exc

    config._base_dir = path.resolve().parent
    return config


def _format_validation_error(path: Path, exc: ValidationError) -> str:
    count = exc.error_count()
    plural = "s" if count != 1 else ""
    lines = [f"{path}: invalid config ({count} error{plural}):"]
    for error in exc.errors():
        loc = ".".join(str(part) for part in error["loc"]) or "<top level>"
        lines.append(f"  {loc}: {error['msg']}")
    return "\n".join(lines)
