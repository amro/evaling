"""What `evaling doctor` reports: the state of an installation, in one place.

The bug-report recipe used to be three commands whose combined output still
left out the things that actually go wrong — which settings layer supplied a
directory, which secrets file is in play, whether the key a model needs
resolves at all. This collects that, and answers the question people ask
before filing anything: *why is it not using what I told it to use?*

Nothing here reaches the network. Provider credentials can only really be
checked by using them, so that is opt-in and separate — see :func:`probe`.

Secret values never appear. Variable *names* do, because a name is the thing
you have to get right, and :func:`evaling.secrets.describe_secrets` is what
decides where that line is.
"""

import asyncio
import os
import platform
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from evaling import __version__
from evaling.cache import ResponseCache
from evaling.config.loader import load_config, load_project_settings
from evaling.config.schema import CaseFileRef, CaseSourceRef, EvalConfig, Settings
from evaling.config.settings import (
    ENV_VARS,
    USER_CONFIG_ENV_VAR,
    default_user_config_path,
    resolve_settings,
)
from evaling.errors import EvalingError
from evaling.providers import create_provider
from evaling.render import RenderedMessage, RenderedText
from evaling.secrets import build_env, describe_secrets
from evaling.storage import RunStore


@dataclass
class Report:
    """Everything doctor found. ``problems`` is what a reader should act on."""

    sections: dict[str, Any]
    problems: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {**self.sections, "problems": self.problems}


def collect(
    config_path: str | Path = "eval.yaml",
    cli_settings: Mapping[str, Any] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Report:
    """Gather the report. Never raises: a broken config is a finding, not a crash."""
    env = os.environ if env is None else env
    problems: list[str] = []
    path = Path(config_path)

    config, config_section = _describe_config(path, problems)
    base_dir = path.resolve().parent if path.is_file() else None
    settings, settings_section = _describe_settings(
        config, path, cli_settings, env, base_dir, problems
    )

    return Report(
        sections={
            "evaling": _describe_install(),
            "config": config_section,
            "settings": settings_section,
            "secrets": _describe_secrets(base_dir, env, problems),
            "models": _describe_models(config, base_dir, env, problems),
            "cache": _describe_cache(settings),
            "runs": _describe_runs(settings, problems),
        },
        problems=problems,
    )


def _describe_install() -> dict[str, Any]:
    return {
        "version": __version__,
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "platform": platform.platform(),
        "mcp_extra": find_spec("mcp") is not None,
    }


def _describe_config(path: Path, problems: list[str]) -> tuple[EvalConfig | None, dict[str, Any]]:
    section: dict[str, Any] = {"path": str(path), "found": path.is_file()}
    if not section["found"]:
        section["error"] = None
        problems.append(f"no config at {path} — commands that need one will fail here")
        return None, section
    try:
        config = load_config(path)
    except EvalingError as exc:
        section["error"] = str(exc)
        problems.append(f"{path} does not load: {exc}")
        return None, section

    section["error"] = None
    section["models"] = [model.id for model in config.models]
    section["variants"] = [variant.name for variant in config.variants]
    section["judges"] = sorted(config.judges)
    section["criteria"] = [criterion.criterion for criterion in config.scorecard]
    section["no_look"] = config.privacy.no_look
    if isinstance(config.cases, CaseSourceRef):
        section["cases"] = f"source {config.cases.source}"
    elif isinstance(config.cases, CaseFileRef):
        section["cases"] = f"file {config.cases.file}"
    else:
        section["cases"] = f"{len(config.cases)} inline"
    return config, section


def _describe_settings(
    config: EvalConfig | None,
    path: Path,
    cli_settings: Mapping[str, Any] | None,
    env: Mapping[str, str],
    base_dir: Path | None,
    problems: list[str],
) -> tuple[Settings, dict[str, Any]]:
    """Resolved settings, each with the layer that supplied it.

    The provenance is the point. "Why are my runs not where I put them" is
    answered by which layer won, and nothing else in evaling shows that.
    """
    eval_settings = config.settings if config is not None else _quiet_project_settings(path)
    try:
        settings = resolve_settings(cli_settings, eval_settings, env=env, base_dir=base_dir)
    except EvalingError as exc:
        problems.append(f"settings do not resolve: {exc}")
        settings = Settings()

    user_settings = _quiet_user_settings(env)
    by_env = {field: var for var, field in ENV_VARS.items() if env.get(var)}
    described = {}
    for field in Settings.model_fields:
        if cli_settings and cli_settings.get(field) is not None:
            source = "command line"
        elif field in by_env:
            source = by_env[field]
        elif eval_settings is not None and field in eval_settings.model_fields_set:
            source = f"config {path}"
        elif user_settings is not None and field in user_settings.model_fields_set:
            source = "user config"
        else:
            source = "default"
        described[field] = {"value": str(getattr(settings, field)), "from": source}
    return settings, described


def _quiet_project_settings(path: Path) -> Settings | None:
    try:
        return load_project_settings(path)
    except EvalingError:
        return None  # already reported against the config itself


def _quiet_user_settings(env: Mapping[str, str]) -> Settings | None:
    explicit = env.get(USER_CONFIG_ENV_VAR)
    target = Path(explicit) if explicit else default_user_config_path()
    try:
        return load_project_settings(target) or _user_config_as_settings(target)
    except EvalingError:
        return None


def _user_config_as_settings(path: Path) -> Settings | None:
    """The user config is a bare settings mapping, not a `settings:` block."""
    from evaling.config.settings import _load_user_config

    try:
        return _load_user_config(path)
    except EvalingError:
        return None


def _describe_secrets(
    base_dir: Path | None, env: Mapping[str, str], problems: list[str]
) -> dict[str, Any]:
    files = describe_secrets(base_dir, env)
    for entry in files:
        if entry["error"]:
            problems.append(f"{entry['path']} could not be read: {entry['error']}")
        if entry["world_readable"]:
            problems.append(
                f"{entry['path']} is readable by other users; consider: chmod 600 {entry['path']}"
            )
    return {"files": files, "env_var": env.get("EVALING_SECRETS")}


def _describe_models(
    config: EvalConfig | None,
    base_dir: Path | None,
    env: Mapping[str, str],
    problems: list[str],
) -> list[dict[str, Any]]:
    if config is None:
        return []
    try:
        secret_env: Mapping[str, str] = build_env(base_dir, env)[0]
    except EvalingError as exc:
        # A broken secrets file is one of the things people run doctor to find,
        # so it must not be the thing that stops doctor from running. It is
        # already reported against the file itself; here it just means keys
        # resolve from the environment alone.
        problems.append(
            f"secrets could not be loaded, so keys below come from the environment only: {exc}"
        )
        secret_env = env
    described = []
    for model in config.models:
        entry: dict[str, Any] = {
            "id": model.id,
            "provider": model.provider,
            "role": model.role,
            "base_url": model.base_url,
            "command": model.command,
        }
        try:
            provider = create_provider(model, secret_env, base_dir)
        except EvalingError as exc:
            entry["error"] = str(exc)
            problems.append(f"model {model.id}: {exc}")
            described.append(entry)
            continue
        key_env = getattr(provider, "api_key_env", None)
        entry["api_key_env"] = key_env or None
        if key_env:
            # Presence only. The value is a secret and stays one.
            entry["api_key_found"] = bool(secret_env.get(key_env))
            if not entry["api_key_found"] and getattr(provider, "REQUIRES_API_KEY", False):
                problems.append(f"model {model.id}: no value for {key_env}")
        described.append(entry)
    return described


def _describe_cache(settings: Settings) -> dict[str, Any]:
    stats = ResponseCache(settings.cache_dir).stats()
    return {"enabled": settings.cache, **stats}


def _describe_runs(settings: Settings, problems: list[str]) -> dict[str, Any]:
    store = RunStore(settings.output_dir)
    section: dict[str, Any] = {"path": str(settings.output_dir), "count": len(store.list_runs())}
    section["baseline"] = store.get_baseline()
    section["writable"] = _writable(settings.output_dir)
    if not section["writable"]:
        problems.append(f"{settings.output_dir} is not writable, so no run can be stored")
    return section


def _writable(path: Path) -> bool:
    """Whether a run could actually be written here, checked by trying."""
    probe = path / ".evaling-write-probe"
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


# -- the opt-in half ------------------------------------------------------


def probe(config: EvalConfig, base_dir: Path | None = None) -> list[dict[str, Any]]:
    """Make one minimal call per model to see whether credentials work.

    Separate and opt-in because it is the only thing in this module that
    reaches the network, and because a real call is the only way to learn
    what a provider thinks of your key. It costs a fraction of a cent per
    model; `evaling doctor` says so before doing it.
    """
    return asyncio.run(_probe_async(config, base_dir))


async def _probe_async(config: EvalConfig, base_dir: Path | None) -> list[dict[str, Any]]:
    from evaling.providers import CompletionRequest

    secret_env, _ = build_env(base_dir)
    results = []
    for model in config.models:
        entry: dict[str, Any] = {"id": model.id, "provider": model.provider}
        provider = None
        try:
            provider = create_provider(model, secret_env, base_dir)
            request = CompletionRequest(
                model=model,
                messages=[RenderedMessage(role="user", parts=(RenderedText(text="ping"),))],
            )
            completion = await provider.complete(request)
            entry["reachable"] = True
            entry["cost_usd"] = completion.cost_usd
        except Exception as exc:  # noqa: BLE001 - every failure is a finding here
            entry["reachable"] = False
            entry["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            if provider is not None:
                await provider.aclose()
        results.append(entry)
    return results
