"""PubMed provider: asks NCBI's E-utilities for papers and returns them as `Paper` objects.

Searching PubMed takes TWO calls:
  1. esearch -> a ranked list of paper IDs (PMIDs) matching the query. No details.
  2. efetch  -> the full records (title, authors, abstract...) for those IDs, as XML.
"""

import httpx

from app.config import Settings
from app.providers.base import ProviderError
from app.providers.http import ProviderHttp
from app.providers.pubmed_parser import PubMedParseError, parse_pubmed_xml
from app.providers.rate_limit import RateLimiter
from app.schemas.paper import Paper

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
ESEARCH_URL = f"{EUTILS}/esearch.fcgi"
EFETCH_URL = f"{EUTILS}/efetch.fcgi"


class PubMedProvider:
    name = "pubmed"  # shows up in `provider_status` and in Paper.source

    def __init__(
        self,
        client: httpx.AsyncClient,
        settings: Settings,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._retmax = settings.pubmed_retmax  # how many papers to ask for (default 20)
        # All the shared HTTP behaviour (retry, timeout, rate limit) lives in ProviderHttp.
        self._http = ProviderHttp(
            self.name,
            client,
            rate_limiter or RateLimiter(settings.pubmed_requests_per_second),
            timeout=settings.http_timeout_seconds,
            backoff=settings.retry_backoff_seconds,
        )
        # Sent with every request so NCBI knows who is calling (they ask for this).
        self._identity = {"tool": settings.ncbi_tool, "email": settings.ncbi_email}
        if settings.ncbi_api_key:
            # Optional key: raises our allowed rate from 3 to 10 requests/second.
            self._identity["api_key"] = settings.ncbi_api_key.get_secret_value()

    async def search(self, query: str) -> list[Paper]:
        pmids = await self._esearch(query)  # call 1: which papers?
        if not pmids:
            return []  # nothing found: skip the second call entirely

        # call 2: get the details of those papers.
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
        """Return the matching PMIDs (as strings), best match first."""
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
        # Be careful with the reply shape: PubMed can answer 200 OK with an error inside.
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

    async def _get(self, url: str, params: dict[str, str | int]) -> httpx.Response:
        # NCBI asks every request to carry tool/email (and the API key when configured).
        return await self._http.get(url, {**self._identity, **params})
