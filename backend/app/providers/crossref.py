"""Crossref provider: searches Crossref's /works endpoint and returns `Paper` objects.

Unlike PubMed, Crossref needs just ONE call, and the reply is JSON that already contains
the details (title, authors, ...), so there is no second "fetch" step.
"""

import httpx

from app.config import Settings
from app.providers.base import ProviderError
from app.providers.crossref_parser import CrossrefParseError, parse_crossref_response
from app.providers.http import ProviderHttp
from app.providers.rate_limit import RateLimiter
from app.schemas.paper import Paper

WORKS_URL = "https://api.crossref.org/works"

# Only the fields the Paper model uses: keeps responses small and fast.
_SELECT = ",".join(
    [
        "DOI",
        "title",
        "author",
        "abstract",
        "container-title",
        "issued",
        "published",
        "published-print",
        "published-online",
        "URL",
    ]
)


class CrossrefProvider:
    name = "crossref"  # shows up in `provider_status` and in Paper.source

    def __init__(
        self,
        client: httpx.AsyncClient,
        settings: Settings,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._rows = settings.crossref_rows  # how many results to ask for (default 20)
        self._mailto = settings.crossref_mailto  # optional contact email -> faster "polite pool"
        self._http = ProviderHttp(
            self.name,
            client,
            rate_limiter or RateLimiter(settings.crossref_requests_per_second),
            timeout=settings.http_timeout_seconds,
            backoff=settings.retry_backoff_seconds,
        )

    async def search(self, query: str) -> list[Paper]:
        params: dict[str, str | int] = {
            "query.bibliographic": query,  # search in titles/authors/journal etc.
            "rows": self._rows,
            "select": _SELECT,
            # Crossref also indexes books, datasets, peer-review reports, etc.
            "filter": "type:journal-article",
        }
        if self._mailto:
            params["mailto"] = self._mailto  # only sent if you configured one; never invented

        response = await self._http.get(WORKS_URL, params)
        try:
            return parse_crossref_response(response.json())
        except (ValueError, CrossrefParseError):  # invalid JSON, or valid JSON of the wrong shape
            raise ProviderError(self.name, "unexpected response") from None
