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


def create_provider(spec: ModelSpec) -> Provider:
    cls = _REGISTRY.get(spec.provider)
    if cls is None:
        raise ProviderError(f"model {spec.id!r}: provider {spec.provider!r} is not implemented yet")
    return cls(spec)
