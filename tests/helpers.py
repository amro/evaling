"""Shared test builders for engine/CLI tests."""

import asyncio
import contextlib

from evaling.config import EvalConfig, Settings


def make_config(tmp_path, models=None, cases=None, variants=None):
    cfg = EvalConfig.model_validate(
        {
            "models": models or [{"id": "m1", "provider": "mock"}],
            "variants": variants
            or [{"name": "v1", "prompt": [{"role": "user", "content": "{{ q }}"}]}],
            "cases": cases
            or [{"id": "c1", "vars": {"q": "alpha"}}, {"id": "c2", "vars": {"q": "beta"}}],
            "scorecard": [{"criterion": "acc", "scorer": {"type": "exact"}}],
        }
    )
    cfg._base_dir = tmp_path
    return cfg


def make_settings(tmp_path, cache=False, concurrency=4):
    return Settings.model_validate(
        {
            "output_dir": str(tmp_path / "runs"),
            "cache_dir": str(tmp_path / "cache"),
            "cache": cache,
            "concurrency": concurrency,
        }
    )


async def loop_ticks_during(coro, interval=0.005):
    """Await ``coro`` while counting event-loop turns; near-zero means blocked.

    The detector for "sync user code ran on the loop": a callable pushed
    through to_thread lets the ticker keep running, one called inline
    freezes it for the call's whole duration.
    """
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(interval)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        result = await coro
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    return ticks, result
