import asyncio
import time


class RateLimiter:
    """Makes sure we don't call an API too often.

    Think of it as a turnstile with two rules:
      1. Only `max_concurrent` requests may be "in flight" at the same time.
      2. Requests must START at least 1/requests_per_second seconds apart.

    Rule 2 is the important one: NCBI and Crossref limit requests *per second*, and a
    concurrency limit alone would not stop us from sending 50 quick requests in a row.

    Usage:
        async with limiter:
            await client.get(...)   # runs only when the turnstile lets us through
    """

    def __init__(self, requests_per_second: float, max_concurrent: int | None = None) -> None:
        self._interval = 1.0 / requests_per_second  # minimum gap between two request starts
        # The semaphore is a counter of free "slots" (rule 1).
        self._semaphore = asyncio.Semaphore(max_concurrent or max(1, int(requests_per_second)))
        # The lock makes sure two requests don't both claim the same start time.
        self._lock = asyncio.Lock()
        self._next_start = 0.0  # earliest time (monotonic clock) the next request may begin

    async def __aenter__(self) -> None:
        await self._semaphore.acquire()  # wait for a free slot
        try:
            # Reserve our start time: either now, or right after the previous request's slot.
            async with self._lock:
                now = time.monotonic()
                start = max(now, self._next_start)
                self._next_start = start + self._interval
            # Wait until our reserved time. asyncio.sleep can wake slightly early on Windows
            # (coarse timers), so we re-check the clock instead of trusting one sleep.
            while (remaining := start - time.monotonic()) > 0:
                await asyncio.sleep(remaining)
        except BaseException:
            # If we get cancelled while waiting, give the slot back so it isn't lost forever.
            self._semaphore.release()
            raise

    async def __aexit__(self, *exc_info: object) -> None:
        self._semaphore.release()  # request finished (or failed): free the slot
