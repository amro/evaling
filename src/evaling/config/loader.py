"""Load and validate eval.yaml files."""

from pathlib import Path

import yaml
from pydantic import ValidationError

from evaling.config.errors import ConfigError
from evaling.config.schema import EvalConfig, Message, Settings


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


def load_project_settings(path: str | Path) -> "Settings | None":
    """Read only the ``settings:`` block of an eval config, if the file exists.

    Lets commands that don't need the full eval (list, show, baseline …)
    honor the project's output/cache directories without requiring a fully
    valid config. Returns None when the file is absent or has no settings.
    Invalid YAML or an invalid settings block is still an error — silently
    ignoring it would make commands look in the wrong directory.
    """
    path = Path(path)
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict) or "settings" not in data:
        return None
    try:
        return Settings.model_validate(data["settings"])
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(path, exc)) from exc


def load_prompt(path: str | Path) -> list[Message]:
    """Load an external prompt file: a YAML list of messages."""
    path = Path(path)
    try:
        raw = path.read_text()
    except FileNotFoundError:
        raise ConfigError(f"prompt file not found: {path}") from None
    except OSError as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc

    if not isinstance(data, list):
        raise ConfigError(
            f"{path}: a prompt file must be a YAML list of messages, got {type(data).__name__}"
        )
    messages = []
    for index, item in enumerate(data):
        try:
            messages.append(Message.model_validate(item))
        except ValidationError as exc:
            first = exc.errors()[0]
            loc = ".".join(str(part) for part in first["loc"])
            detail = f"{loc}: {first['msg']}" if loc else first["msg"]
            raise ConfigError(f"{path}: message {index + 1}: {detail}") from exc
    if not messages:
        raise ConfigError(f"{path}: prompt file contains no messages")
    return messages


def resolve_prompt(prompt: str | list[Message], base_dir: Path) -> list[Message]:
    """Return a prompt's messages, loading the external file if referenced."""
    if isinstance(prompt, str):
        return load_prompt(base_dir / prompt)
    return prompt


def _format_validation_error(path: Path, exc: ValidationError) -> str:
    count = exc.error_count()
    plural = "s" if count != 1 else ""
    lines = [f"{path}: invalid config ({count} error{plural}):"]
    for error in exc.errors():
        loc = ".".join(str(part) for part in error["loc"]) or "<top level>"
        lines.append(f"  {loc}: {error['msg']}")
    return "\n".join(lines)
