import asyncio

import pytest

from evaling.concurrency import bounded_gather


def test_results_in_input_order():
    async def make(value, delay):
        await asyncio.sleep(delay)
        return value

    factories = [
        lambda: make("slow", 0.02),
        lambda: make("fast", 0.0),
        lambda: make("mid", 0.01),
    ]
    assert asyncio.run(bounded_gather(factories, limit=3)) == ["slow", "fast", "mid"]


def test_concurrency_never_exceeds_limit():
    active = 0
    peak = 0

    async def task():
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.001)
        active -= 1
        return True

    results = asyncio.run(bounded_gather([task for _ in range(20)], limit=3))
    assert len(results) == 20
    assert peak <= 3


def test_limit_one_serializes():
    order = []

    def factory(n):
        async def task():
            order.append(("start", n))
            await asyncio.sleep(0)
            order.append(("end", n))

        return task

    asyncio.run(bounded_gather([factory(n) for n in range(3)], limit=1))
    assert order == [
        ("start", 0),
        ("end", 0),
        ("start", 1),
        ("end", 1),
        ("start", 2),
        ("end", 2),
    ]


def test_exception_propagates():
    async def ok():
        return 1

    async def bad():
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        asyncio.run(bounded_gather([ok, bad], limit=2))


def test_invalid_limit_rejected():
    with pytest.raises(ValueError, match="limit"):
        asyncio.run(bounded_gather([], limit=0))


def test_empty_input():
    assert asyncio.run(bounded_gather([], limit=4)) == []
