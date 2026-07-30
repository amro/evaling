"""The provider interface: how evaling calls models.

Providers are async and pluggable: the engine only ever sees this interface,
so new transports (HTTP APIs, subprocesses, future MCP sampling) slot in
without engine changes.
"""

import math
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from evaling.config.schema import ModelSpec
from evaling.errors import EvalingError
from evaling.render import RenderedMessage


class ProviderError(EvalingError):
    """A model call failed.

    ``retryable`` distinguishes transient failures (rate limits, timeouts,
    5xx) worth retrying from permanent ones (bad request, auth) that are not.
    ``retry_after`` carries a server-supplied wait (seconds) when there is one,
    so backoff can respect a rate limiter instead of guessing.
    """

    def __init__(self, message: str, *, retryable: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


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

    def __post_init__(self):
        # Usage reaches this constructor unvalidated — a command script's
        # stdout, a cache entry on disk. A string token count used to survive
        # until the run's totals added it to an int, and that TypeError killed
        # the whole run instead of one cell. Validate here so every provider
        # and the cache get the same guarantee.
        self.input_tokens = _usage_number("input_tokens", self.input_tokens, integral=True)
        self.output_tokens = _usage_number("output_tokens", self.output_tokens, integral=True)
        self.cost_usd = _usage_number("cost_usd", self.cost_usd, integral=False)


def _usage_number(name: str, value: Any, *, integral: bool) -> int | float | None:
    """Coerce a usage field from whatever JSON carried it in as.

    The obvious numeric spellings ("12", 12.0) are accepted; anything else
    raises ProviderError, which the engine records as that cell's error.
    """
    if value is None:
        return None
    number = None
    if isinstance(value, (int, float, str)) and not isinstance(value, bool):
        try:
            number = float(value)
        except ValueError:
            number = None
    if number is None or not math.isfinite(number) or number < 0:
        raise ProviderError(f"completion reported {name}={value!r}, expected a non-negative number")
    if not integral:
        return number
    if not number.is_integer():
        raise ProviderError(
            f"completion reported {name}={value!r}, expected a whole number of tokens"
        )
    return int(number)


class Provider(ABC):
    """One instance is created per configured model for the duration of a run.

    ``Completion.raw`` is provider debug metadata: it round-trips through the
    response cache but is deliberately not persisted into run results.
    """

    #: Media part kinds this provider can send to its API. Declared on the
    #: class so unsupported parts fail at validation/dry-run time, not mid-run.
    SUPPORTED_MEDIA: ClassVar[frozenset[str]] = frozenset()

    def __init__(
        self,
        spec: ModelSpec,
        *,
        env: "Mapping[str, str] | None" = None,
        base_dir: "Path | None" = None,
        request_log: "Any | None" = None,
    ):
        self.spec = spec
        #: Optional evaling.reqlog.RequestLog. When set, providers write the
        #: request they sent and the response they got. None by default: this
        #: is a debugging aid, not part of a run.
        self.request_log = request_log
        #: Environment used for API-key lookups: the real environment plus any
        #: secrets file. None means "use os.environ".
        self.env = env
        #: Directory of the config that declared this model. Relative paths in
        #: a model spec resolve against it, like every other path in a config.
        self.base_dir = base_dir

    @abstractmethod
    async def complete(self, request: CompletionRequest) -> Completion:
        """Run one model call. Raise ProviderError on failure.

        Concurrency contract: one instance serves many concurrent complete()
        calls on a single event loop (no threads). Any instance state mutated
        across an ``await`` must be guarded by the implementation.
        """

    async def aclose(self) -> None:  # noqa: B027 - optional hook, default no-op
        """Release resources (HTTP pools etc.). Called once after a run."""
