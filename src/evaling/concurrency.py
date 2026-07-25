"""Bounded-concurrency execution for model calls."""

import asyncio
from collections import Counter
from collections.abc import (
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Sequence,
)
from contextlib import asynccontextmanager
from typing import TypeVar

T = TypeVar("T")


async def bounded_gather(factories: Sequence[Callable[[], Awaitable[T]]], limit: int) -> list[T]:
    """Run awaitable factories with at most ``limit`` in flight.

    Results come back in input order. Exceptions propagate (first one wins),
    cancelling the rest — callers that need per-item error capture should
    catch inside the factory.

    Materializes one task per factory, so this is only appropriate for small
    fixed collections. For a matrix sized by user data, use
    :func:`consume_bounded`.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    semaphore = asyncio.Semaphore(limit)

    async def run(factory: Callable[[], Awaitable[T]]) -> T:
        async with semaphore:
            return await factory()

    return await asyncio.gather(*(run(factory) for factory in factories))


class KeyedLocks:
    """A lock per key, held only while someone wants it.

    Single-flights concurrent work on the same key: the second caller waits and
    then finds the first caller's result. Locks are refcounted and dropped when
    the last waiter leaves, so a long run doesn't accumulate one dead lock per
    distinct key it has ever seen.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._waiters: Counter[str] = Counter()

    def __len__(self) -> int:
        return len(self._locks)

    @asynccontextmanager
    async def __call__(self, key: str) -> AsyncIterator[None]:
        lock = self._locks.setdefault(key, asyncio.Lock())
        self._waiters[key] += 1
        try:
            async with lock:
                yield
        finally:
            self._waiters[key] -= 1
            if not self._waiters[key]:
                del self._waiters[key]
                del self._locks[key]


async def consume_bounded(
    factories: Iterable[Callable[[], Awaitable[T]]] | AsyncIterable[Callable[[], Awaitable[T]]],
    limit: int,
    handle: Callable[[T], None],
) -> None:
    """Run factories from an iterable, at most ``limit`` in flight, streaming results.

    Unlike :func:`bounded_gather`, this never holds more than ``limit`` tasks
    or results at once: a fixed pool of workers pulls from the iterable, and
    each result is handed to ``handle`` and then dropped. Peak memory becomes a
    function of the concurrency limit rather than of the number of items, which
    is what lets a run cover hundreds of thousands of cells.

    ``handle`` is called in completion order, not input order. Exceptions from
    a factory or from ``handle`` propagate and cancel the remaining workers.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")

    if hasattr(factories, "__aiter__"):
        await _consume_async(factories, limit, handle)
        return

    iterator = iter(factories)

    async def worker() -> None:
        while True:
            # next() is synchronous and asyncio does not switch tasks
            # mid-statement, so workers cannot race for the same item.
            try:
                factory = next(iterator)
            except StopIteration:
                return
            handle(await factory())

    await asyncio.gather(*(worker() for _ in range(limit)))


async def _consume_async(
    factories: AsyncIterable[Callable[[], Awaitable[T]]],
    limit: int,
    handle: Callable[[T], None],
) -> None:
    """Same, for a source that is itself awaited (a paging API, say).

    ``__anext__`` suspends, so unlike the sync path the workers really can race
    and an async generator raises if re-entered while running — hence the lock.
    """
    iterator = factories.__aiter__()
    lock = asyncio.Lock()

    async def worker() -> None:
        while True:
            async with lock:
                try:
                    factory = await iterator.__anext__()
                except StopAsyncIteration:
                    return
            handle(await factory())

    await asyncio.gather(*(worker() for _ in range(limit)))
