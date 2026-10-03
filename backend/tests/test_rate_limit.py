import asyncio
import time

from app.providers.rate_limit import RateLimiter


async def test_request_starts_are_spaced():
    limiter = RateLimiter(requests_per_second=20, max_concurrent=5)
    starts: list[float] = []

    async def hit():
        async with limiter:
            starts.append(time.monotonic())

    await asyncio.gather(*(hit() for _ in range(4)))
    # 4 starts at 20/s need >= 3 intervals (0.15 s). Individual gaps are noisy on Windows
    # (~15 ms timer granularity), so assert on the total span with that much slack.
    assert max(starts) - min(starts) >= 0.13, starts


async def test_concurrency_is_bounded():
    limiter = RateLimiter(requests_per_second=1000, max_concurrent=2)
    running = peak = 0

    async def hit():
        nonlocal running, peak
        async with limiter:
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.02)
            running -= 1

    await asyncio.gather(*(hit() for _ in range(6)))
    assert peak == 2


async def test_slot_is_released_after_error():
    limiter = RateLimiter(requests_per_second=1000, max_concurrent=1)
    try:
        async with limiter:
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    async def reacquire():
        async with limiter:
            pass

    await asyncio.wait_for(reacquire(), timeout=1)
