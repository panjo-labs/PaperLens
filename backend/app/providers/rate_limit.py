import asyncio
import time


class RateLimiter:
    """Async context manager bounding both concurrency and request start rate.

    A semaphore alone only caps in-flight requests; NCBI limits requests per second,
    so starts are also spaced at least 1/requests_per_second apart.
    """

    def __init__(self, requests_per_second: float, max_concurrent: int | None = None) -> None:
        self._interval = 1.0 / requests_per_second
        self._semaphore = asyncio.Semaphore(max_concurrent or max(1, int(requests_per_second)))
        self._lock = asyncio.Lock()
        self._next_start = 0.0

    async def __aenter__(self) -> None:
        await self._semaphore.acquire()
        try:
            async with self._lock:
                now = time.monotonic()
                start = max(now, self._next_start)
                self._next_start = start + self._interval
            # asyncio.sleep can wake slightly early (coarse timers on Windows); re-check the clock.
            while (remaining := start - time.monotonic()) > 0:
                await asyncio.sleep(remaining)
        except BaseException:
            self._semaphore.release()
            raise

    async def __aexit__(self, *exc_info: object) -> None:
        self._semaphore.release()
