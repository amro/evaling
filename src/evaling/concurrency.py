"""Bounded-concurrency execution for model calls."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import TypeVar

T = TypeVar("T")


async def bounded_gather(factories: Sequence[Callable[[], Awaitable[T]]], limit: int) -> list[T]:
    """Run awaitable factories with at most ``limit`` in flight.

    Results come back in input order. Exceptions propagate (first one wins),
    cancelling the rest — callers that need per-item error capture should
    catch inside the factory.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    semaphore = asyncio.Semaphore(limit)

    async def run(factory: Callable[[], Awaitable[T]]) -> T:
        async with semaphore:
            return await factory()

    return await asyncio.gather(*(run(factory) for factory in factories))
