"""Retry transient provider failures with exponential backoff."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from evaling.providers.base import ProviderError

T = TypeVar("T")


async def call_with_retries(
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    max_retry_after: float = 60.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Call fn, retrying retryable ProviderErrors with exponential backoff.

    Delays are base_delay * 2^(attempt-1), capped at max_delay — unless the
    error carries a server-supplied ``retry_after`` (e.g. a 429's Retry-After
    header), which is honored up to ``max_retry_after``. Guessing a 1s backoff
    when the API asked for 30s just burns the remaining attempts.

    Non-retryable errors and the final failed attempt propagate unchanged.
    ``sleep`` is injectable so tests don't wait.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    attempt = 1
    while True:
        try:
            return await fn()
        except ProviderError as exc:
            if not exc.retryable or attempt >= max_attempts:
                raise
            hinted = getattr(exc, "retry_after", None)
            if hinted is not None and hinted >= 0:
                delay = min(float(hinted), max_retry_after)
            else:
                delay = min(base_delay * 2 ** (attempt - 1), max_delay)
            await sleep(delay)
            attempt += 1
