"""The provider interface: how evaling calls models.

Providers are async and pluggable: the engine only ever sees this interface,
so new transports (HTTP APIs, subprocesses, future MCP sampling) slot in
without engine changes.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from evaling.config.schema import ModelSpec
from evaling.errors import EvalingError
from evaling.render import RenderedMessage


class ProviderError(EvalingError):
    """A model call failed.

    ``retryable`` distinguishes transient failures (rate limits, timeouts,
    5xx) worth retrying from permanent ones (bad request, auth) that are not.
    """

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class CompletionRequest:
    model: ModelSpec
    messages: list[RenderedMessage]


@dataclass
class Completion:
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class Provider(ABC):
    """One instance is created per configured model for the duration of a run.

    ``Completion.raw`` is provider debug metadata: it round-trips through the
    response cache but is deliberately not persisted into run results.
    """

    #: Media part kinds this provider can send to its API. Declared on the
    #: class so unsupported parts fail at validation/dry-run time, not mid-run.
    SUPPORTED_MEDIA: ClassVar[frozenset[str]] = frozenset()

    def __init__(self, spec: ModelSpec):
        self.spec = spec

    @abstractmethod
    async def complete(self, request: CompletionRequest) -> Completion:
        """Run one model call. Raise ProviderError on failure.

        Concurrency contract: one instance serves many concurrent complete()
        calls on a single event loop (no threads). Any instance state mutated
        across an ``await`` must be guarded by the implementation.
        """

    async def aclose(self) -> None:  # noqa: B027 - optional hook, default no-op
        """Release resources (HTTP pools etc.). Called once after a run."""
