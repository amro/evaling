import asyncio

import pytest

from evaling.providers.base import ProviderError
from evaling.providers.retry import call_with_retries


class Flaky:
    def __init__(self, fail_times, retryable=True):
        self.fail_times = fail_times
        self.retryable = retryable
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ProviderError(f"boom #{self.calls}", retryable=self.retryable)
        return "ok"


def run(coro):
    return asyncio.run(coro)


def make_sleep_recorder(delays):
    async def sleep(delay):
        delays.append(delay)

    return sleep


def test_succeeds_first_try_no_sleep():
    delays = []
    fn = Flaky(0)
    assert run(call_with_retries(fn, sleep=make_sleep_recorder(delays))) == "ok"
    assert fn.calls == 1
    assert delays == []


def test_retries_transient_failures_with_exponential_backoff():
    delays = []
    fn = Flaky(2)
    result = run(
        call_with_retries(fn, max_attempts=3, base_delay=0.5, sleep=make_sleep_recorder(delays))
    )
    assert result == "ok"
    assert fn.calls == 3
    assert delays == [0.5, 1.0]


def test_delay_capped_at_max_delay():
    delays = []
    fn = Flaky(4)
    run(
        call_with_retries(
            fn, max_attempts=5, base_delay=1.0, max_delay=3.0, sleep=make_sleep_recorder(delays)
        )
    )
    assert delays == [1.0, 2.0, 3.0, 3.0]


def test_gives_up_after_max_attempts():
    fn = Flaky(10)
    with pytest.raises(ProviderError, match="boom #3"):
        run(call_with_retries(fn, max_attempts=3, sleep=make_sleep_recorder([])))
    assert fn.calls == 3


def test_non_retryable_error_propagates_immediately():
    fn = Flaky(10, retryable=False)
    with pytest.raises(ProviderError, match="boom #1"):
        run(call_with_retries(fn, max_attempts=5, sleep=make_sleep_recorder([])))
    assert fn.calls == 1


def test_non_provider_errors_propagate_immediately():
    async def explode():
        raise RuntimeError("not a provider error")

    with pytest.raises(RuntimeError):
        run(call_with_retries(explode, sleep=make_sleep_recorder([])))


def test_zero_attempts_rejected():
    with pytest.raises(ValueError, match="max_attempts"):
        run(call_with_retries(Flaky(0), max_attempts=0))
