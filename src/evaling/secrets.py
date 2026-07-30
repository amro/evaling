"""Secrets from a file that never gets committed.

Provider API keys normally come from environment variables. That is awkward
for a project you return to weekly, so evaling also reads a plain YAML file of
``ENV_VAR: value`` pairs:

    # .evaling.secrets.yaml — never commit this
    ANTHROPIC_API_KEY: sk-ant-...
    OPENAI_API_KEY: sk-...

Lookup order, first hit wins:

1. the real environment (so CI and one-off overrides always win)
2. ``$EVALING_SECRETS``, if set
3. ``.evaling.secrets.yaml`` beside the eval config
4. ``~/.config/evaling/secrets.yaml``

Values are merged into a mapping handed to providers — never into
``os.environ``, so they cannot leak into unrelated subprocesses. ``evaling
init`` writes a ``.gitignore`` covering the project file, and evaling warns
if a secrets file is readable by other users.
"""

import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from evaling.errors import EvalingError
from evaling.textfile import read_yaml

PROJECT_SECRETS_NAME = ".evaling.secrets.yaml"
ENV_VAR = "EVALING_SECRETS"


class SecretsError(EvalingError):
    """A secrets file could not be read."""


def user_secrets_path() -> Path:
    return Path("~/.config/evaling/secrets.yaml").expanduser()


def candidate_paths(
    base_dir: Path | None = None, env: Mapping[str, str] | None = None
) -> list[Path]:
    """Secrets files to consult, highest precedence first."""
    env = os.environ if env is None else env
    paths: list[Path] = []
    explicit = env.get(ENV_VAR)
    if explicit:
        paths.append(Path(explicit).expanduser())
    if base_dir is not None:
        paths.append(Path(base_dir) / PROJECT_SECRETS_NAME)
    paths.append(user_secrets_path())
    return paths


def _load_file(path: Path) -> dict[str, str]:
    # quote_content=False: a syntax error in this file is a syntax error on a
    # line that holds a credential, and PyYAML quotes the line it failed on.
    data = read_yaml(
        path, SecretsError, missing=f"secrets file not found: {path}", quote_content=False
    )
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise SecretsError(
            f"{path}: expected a mapping of ENV_VAR: value, got {type(data).__name__}"
        )
    values: dict[str, str] = {}
    for key, value in data.items():
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            raise SecretsError(f"{path}: {key} must be a scalar, got {type(value).__name__}")
        values[str(key)] = str(value)
    return values


def world_readable(path: Path) -> bool:
    """True when a secrets file is readable by group or other.

    POSIX only. Windows reports synthesized mode bits that always look
    group- and world-readable, so checking them there would warn on every
    run while saying nothing about the real ACL.
    """
    if os.name != "posix":
        return False
    try:
        mode = path.stat().st_mode
    except OSError:  # pragma: no cover - raced away between checks
        return False
    return bool(mode & (stat.S_IRGRP | stat.S_IROTH))


def load_secrets(
    base_dir: Path | None = None, env: Mapping[str, str] | None = None
) -> tuple[dict[str, str], list[str]]:
    """Merge every secrets file that exists. Returns (values, warnings)."""
    merged: dict[str, str] = {}
    warnings: list[str] = []
    explicit = (os.environ if env is None else env).get(ENV_VAR)
    for path in candidate_paths(base_dir, env):
        if not path.is_file():
            # A path named explicitly by the user should not silently do nothing.
            if explicit and str(path) == str(Path(explicit).expanduser()):
                raise SecretsError(f"{ENV_VAR} points at a missing file: {path}")
            continue
        for key, value in _load_file(path).items():
            merged.setdefault(key, value)  # earlier paths win
        if world_readable(path):
            warnings.append(f"{path} is readable by other users; consider: chmod 600 {path}")
    return merged, warnings


def describe_secrets(
    base_dir: Path | None = None, env: Mapping[str, str] | None = None
) -> list[dict[str, Any]]:
    """Which secrets files are in play and which variables each defines.

    Names only — never values. Diagnostics live in another module, and the
    decision that a secrets file may be *described* but not *quoted* belongs
    here, next to the loading, rather than being re-decided by every caller
    that wants to be helpful about configuration.
    """
    described = []
    for path in candidate_paths(base_dir, env):
        if not path.is_file():
            continue
        try:
            keys = sorted(_load_file(path))
            error = None
        except SecretsError as exc:
            keys, error = [], str(exc)
        described.append(
            {
                "path": str(path),
                "keys": keys,
                "error": error,
                "world_readable": world_readable(path),
            }
        )
    return described


class SecretEnv(Mapping):
    """Environment lookups that fall back to secrets files.

    Providers receive this instead of ``os.environ``; real environment
    variables take precedence, so CI keeps working unchanged.
    """

    def __init__(self, env: Mapping[str, str], secrets: Mapping[str, str]):
        self._env = env
        self._secrets = secrets

    def __getitem__(self, key: str) -> str:
        if key in self._env:
            return self._env[key]
        return self._secrets[key]

    def __iter__(self):
        seen = set(self._env)
        yield from self._env
        for key in self._secrets:
            if key not in seen:
                yield key

    def __len__(self) -> int:
        return len(set(self._env) | set(self._secrets))

    @property
    def secret_values(self) -> list[str]:
        """Every secret value, for redaction. Never log this."""
        return [value for value in self._secrets.values() if value]


def build_env(
    base_dir: Path | None = None, env: Mapping[str, str] | None = None
) -> tuple[SecretEnv, list[str]]:
    """The environment providers should use, plus any warnings to surface."""
    base = os.environ if env is None else env
    secrets, warnings = load_secrets(base_dir, base)
    return SecretEnv(base, secrets), warnings


def redact(text: str, values: Any) -> str:
    """Replace any known secret value appearing in text."""
    for value in values or []:
        if value and len(value) >= 8 and value in text:
            text = text.replace(value, "<redacted>")
    return text
