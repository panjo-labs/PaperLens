import asyncio
import logging

import httpx

from app.config import Settings
from app.providers.base import ProviderError
from app.providers.pubmed_parser import PubMedParseError, parse_pubmed_xml
from app.providers.rate_limit import RateLimiter
from app.schemas.paper import Paper

logger = logging.getLogger(__name__)

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
ESEARCH_URL = f"{EUTILS}/esearch.fcgi"
EFETCH_URL = f"{EUTILS}/efetch.fcgi"

_MAX_ATTEMPTS = 2  # one retry on 429 / 5xx
_MAX_RETRY_AFTER_SECONDS = 5.0  # never wait longer than this on a server-supplied Retry-After


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class PubMedProvider:
    name = "pubmed"

    def __init__(
        self,
        client: httpx.AsyncClient,
        settings: Settings,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._client = client
        self._retmax = settings.pubmed_retmax
        self._backoff = settings.retry_backoff_seconds
        self._timeout = settings.http_timeout_seconds
        self._limiter = rate_limiter or RateLimiter(settings.pubmed_requests_per_second)
        self._identity = {"tool": settings.ncbi_tool, "email": settings.ncbi_email}
        if settings.ncbi_api_key:
            self._identity["api_key"] = settings.ncbi_api_key.get_secret_value()

    async def search(self, query: str) -> list[Paper]:
        pmids = await self._esearch(query)
        if not pmids:
            return []

        response = await self._get(
            EFETCH_URL, {"db": "pubmed", "id": ",".join(pmids), "retmode": "xml"}
        )
        try:
            papers = parse_pubmed_xml(response.content)
        except PubMedParseError:
            raise ProviderError(self.name, "response could not be parsed") from None

        # efetch does not guarantee the order of the requested IDs; keep esearch's relevance order.
        by_id = {paper.source_id: paper for paper in papers}
        return [by_id[pmid] for pmid in pmids if pmid in by_id]

    async def _esearch(self, query: str) -> list[str]:
        response = await self._get(
            ESEARCH_URL,
            {
                "db": "pubmed",
                "term": query,
                "retmax": self._retmax,
                "retmode": "json",
                # E-utilities default to ID (recency) order, not relevance.
                "sort": "relevance",
            },
        )
        try:
            result = response.json()["esearchresult"]
        except (ValueError, KeyError, TypeError):
            raise ProviderError(self.name, "unexpected search response") from None
        if not isinstance(result, dict) or "ERROR" in result:
            raise ProviderError(self.name, "search request was rejected")

        ids = result.get("idlist", [])
        if not isinstance(ids, list):
            raise ProviderError(self.name, "unexpected search response")
        return [str(pmid) for pmid in ids]

    def _retry_delay(self, response: httpx.Response) -> float:
        """Default backoff; on 429, honour a delta-seconds Retry-After up to a safe cap."""
        if response.status_code == 429:
            header = response.headers.get("Retry-After", "").strip()
            if header.isdigit():  # the HTTP-date form is ignored and falls back to the default
                return min(max(float(header), self._backoff), _MAX_RETRY_AFTER_SECONDS)
        return self._backoff

    async def _get(self, url: str, params: dict[str, str | int]) -> httpx.Response:
        # Error messages are built by hand: httpx exceptions embed the URL, which carries the API key.
        full_params = {**self._identity, **params}
        for attempt in range(_MAX_ATTEMPTS):
            async with self._limiter:
                try:
                    response = await self._client.get(
                        url, params=full_params, timeout=self._timeout
                    )
                except httpx.TimeoutException:
                    raise ProviderError(self.name, "request timed out") from None
                except httpx.HTTPError:
                    raise ProviderError(self.name, "network error") from None

            status = response.status_code
            if status == 429 or status >= 500:
                if attempt + 1 < _MAX_ATTEMPTS:
                    logger.warning("PubMed returned HTTP %s, retrying once", status)
                    await _sleep(self._retry_delay(response))
                    continue
                raise ProviderError(self.name, f"HTTP {status} after retry")
            if status >= 400:
                raise ProviderError(self.name, f"HTTP {status}")
            return response

        raise AssertionError("unreachable")  # pragma: no cover
