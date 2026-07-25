"""Provider registry: map a model spec to a Provider instance."""

from collections.abc import Mapping
from pathlib import Path

from evaling.config.schema import ModelSpec
from evaling.providers.anthropic import AnthropicProvider
from evaling.providers.base import Completion, CompletionRequest, Provider, ProviderError
from evaling.providers.command import CommandProvider
from evaling.providers.mock import MockProvider
from evaling.providers.openai import OpenAICompatibleProvider, OpenAIProvider

_REGISTRY: dict[str, type[Provider]] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "openai-compatible": OpenAICompatibleProvider,
    "command": CommandProvider,
    "mock": MockProvider,
}

__all__ = [
    "Completion",
    "CompletionRequest",
    "Provider",
    "ProviderError",
    "create_provider",
]


def provider_class(name: str) -> type[Provider]:
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ProviderError(f"provider {name!r} is not implemented yet")
    return cls


def create_provider(
    spec: ModelSpec,
    env: Mapping[str, str] | None = None,
    base_dir: "Path | None" = None,
) -> Provider:
    """Build a provider.

    ``env`` supplies API keys (real environment plus any secrets file);
    providers that don't read the environment ignore it. ``base_dir`` is the
    config's directory, which the ``command`` provider runs its script in.
    """
    try:
        cls = provider_class(spec.provider)
    except ProviderError:
        raise ProviderError(
            f"model {spec.id!r}: provider {spec.provider!r} is not implemented yet"
        ) from None
    return cls(spec, env=env, base_dir=base_dir)
