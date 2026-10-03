"""HTTP behaviour shared by all providers: rate limiting, timeout, one retry, Retry-After."""

import asyncio
import logging

import httpx

from app.providers.base import ProviderError
from app.providers.rate_limit import RateLimiter

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 2  # one retry on 429 / 5xx
MAX_RETRY_AFTER_SECONDS = 5.0  # never wait longer than this on a server-supplied Retry-After


# A tiny wrapper around asyncio.sleep so tests can replace *only* the retry wait.
# (Patching asyncio.sleep itself would also break the rate limiter's waiting.)
async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class ProviderHttp:
    """GET helper that turns every failure into a ProviderError with a safe message.

    Messages are written by hand, never taken from httpx exceptions: those embed the full
    request URL, which can carry credentials (the NCBI API key).
    """

    def __init__(
        self,
        provider: str,
        client: httpx.AsyncClient,
        limiter: RateLimiter,
        *,
        timeout: float,
        backoff: float,
    ) -> None:
        self._provider = provider  # name used in error messages, e.g. "pubmed"
        self._client = client  # one shared client for the whole app (connection reuse)
        self._limiter = limiter  # keeps us under the API's requests-per-second limit
        self._timeout = timeout  # seconds to wait for ONE HTTP request
        self._backoff = backoff  # default pause before the single retry

    async def get(self, url: str, params: dict[str, str | int]) -> httpx.Response:
        # Loop = "try, and if the server says 'too busy' or 'broken', try one more time".
        for attempt in range(MAX_ATTEMPTS):
            async with self._limiter:  # every attempt (including the retry) respects the rate limit
                try:
                    response = await self._client.get(url, params=params, timeout=self._timeout)
                except httpx.TimeoutException:
                    # Timeouts are NOT retried: retrying would double the wait for the user.
                    raise ProviderError(self._provider, "request timed out") from None
                except httpx.HTTPError:
                    raise ProviderError(self._provider, "network error") from None

            status = response.status_code
            if status == 429 or status >= 500:
                # 429 = "you are sending too many requests", 5xx = "the server has a problem".
                # Both are usually temporary, so retry once.
                if attempt + 1 < MAX_ATTEMPTS:
                    logger.warning("%s returned HTTP %s, retrying once", self._provider, status)
                    await _sleep(self._retry_delay(response))
                    continue
                raise ProviderError(self._provider, f"HTTP {status} after retry")
            if status >= 400:
                # Other 4xx errors mean *our request* is wrong; retrying would just fail again.
                raise ProviderError(self._provider, f"HTTP {status}")
            return response  # success

        raise AssertionError("unreachable")  # pragma: no cover

    def _retry_delay(self, response: httpx.Response) -> float:
        """Default backoff; on 429, honour a delta-seconds Retry-After up to a safe cap."""
        if response.status_code == 429:
            # "Retry-After: 3" is the server telling us how many seconds to wait.
            header = response.headers.get("Retry-After", "").strip()
            if header.isdigit():  # the HTTP-date form is ignored and falls back to the default
                # Wait at least our normal backoff, but never more than the safety cap.
                return min(max(float(header), self._backoff), MAX_RETRY_AFTER_SECONDS)
        return self._backoff
