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
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Call fn, retrying retryable ProviderErrors with exponential backoff.

    Delays are base_delay * 2^(attempt-1), capped at max_delay. Non-retryable
    errors and the final failed attempt propagate unchanged. ``sleep`` is
    injectable so tests don't wait.
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
            delay = min(base_delay * 2 ** (attempt - 1), max_delay)
            await sleep(delay)
            attempt += 1
