"""Per-model execution limits: concurrency and request rate.

The global ``concurrency`` setting is one number for the whole matrix, which
is wrong as soon as a matrix mixes a local model with a rate-limited API.
These limits are per model and compose with it: a call must satisfy the global
semaphore, this model's concurrency cap, and this model's rate limit.

Retry-after backoff reacts to a 429 that already happened; a rate limit avoids
sending it in the first place.
"""

import asyncio
import time
from collections import deque


class ModelLimiter:
    """Concurrency cap + sliding-window rate limit for one model."""

    def __init__(
        self,
        max_concurrency: int | None = None,
        requests_per_minute: int | None = None,
        *,
        now=time.monotonic,
        sleep=asyncio.sleep,
    ):
        self._semaphore = asyncio.Semaphore(max_concurrency) if max_concurrency else None
        self._rpm = requests_per_minute
        self._now = now
        self._sleep = sleep
        self._recent: deque[float] = deque()
        self._lock = asyncio.Lock()

    @property
    def unlimited(self) -> bool:
        return self._semaphore is None and not self._rpm

    async def __aenter__(self) -> "ModelLimiter":
        if self._semaphore is not None:
            await self._semaphore.acquire()
        try:
            await self._await_rate_slot()
        except BaseException:
            if self._semaphore is not None:
                self._semaphore.release()
            raise
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._semaphore is not None:
            self._semaphore.release()

    async def _await_rate_slot(self) -> None:
        """Sliding window: wait until fewer than rpm requests are in the last minute."""
        if not self._rpm:
            return
        while True:
            async with self._lock:
                cutoff = self._now() - 60.0
                while self._recent and self._recent[0] <= cutoff:
                    self._recent.popleft()
                if len(self._recent) < self._rpm:
                    self._recent.append(self._now())
                    return
                # Wait for the oldest request to age out of the window.
                delay = self._recent[0] - cutoff
            await self._sleep(max(delay, 0.01))


def limiter_for(spec) -> ModelLimiter:
    """Build the limiter described by a model spec."""
    return ModelLimiter(spec.max_concurrency, spec.requests_per_minute)
