"""Provider registry: map a model spec to a Provider instance."""

from evaling.config.schema import ModelSpec
from evaling.providers.base import Completion, CompletionRequest, Provider, ProviderError
from evaling.providers.mock import MockProvider

_REGISTRY: dict[str, type[Provider]] = {
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


def create_provider(spec: ModelSpec) -> Provider:
    try:
        cls = provider_class(spec.provider)
    except ProviderError:
        raise ProviderError(
            f"model {spec.id!r}: provider {spec.provider!r} is not implemented yet"
        ) from None
    return cls(spec)
