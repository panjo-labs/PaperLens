import asyncio
import logging
from collections.abc import Callable, Sequence

from app.providers.base import AcademicSearchProvider, ProviderError
from app.schemas.paper import Paper
from app.schemas.research import ProviderStatus, ResearchResponse
from app.services.query_processor import ProcessedQuery, process_question

logger = logging.getLogger(__name__)


class ResearchOrchestrator:
    """Runs the research workflow. Currently: question -> query -> provider search -> papers.

    It only knows about the generic `AcademicSearchProvider` interface, never about PubMed or
    Crossref specifically. Adding a new source = adding it to the `providers` list.
    """

    def __init__(
        self,
        providers: Sequence[AcademicSearchProvider],
        query_processor: Callable[[str], ProcessedQuery] = process_question,
        provider_time_budget: float | None = None,
    ) -> None:
        self._providers = list(providers)
        self._process = query_processor
        # Max seconds ONE provider may take in total (None = no limit, used mainly in tests).
        self._budget = provider_time_budget

    async def research(self, question: str) -> ResearchResponse:
        """Raises InvalidQueryError if the question has no searchable terms."""
        # Step 1: turn the question into a search query.
        processed = self._process(question)

        # Step 2: search ALL providers at the same time. gather() starts them together and
        # waits for all of them, so total time = the slowest provider, not the sum.
        # Results come back in the same order as `self._providers`, however fast each one was.
        outcomes = await asyncio.gather(
            *(self._search_one(provider, processed.search_query) for provider in self._providers)
        )

        # Step 3: combine. Papers are simply concatenated (no ranking/dedup yet), and each
        # provider gets its own entry in `statuses` so the caller can see who worked.
        papers: list[Paper] = []
        statuses: dict[str, ProviderStatus] = {}
        for provider, (provider_papers, status) in zip(self._providers, outcomes):
            papers.extend(provider_papers)
            statuses[provider.name] = status

        return ResearchResponse(
            question=processed.original,
            queries_used=[processed.search_query],
            papers=papers,
            provider_status=statuses,
        )

    async def _search_one(
        self, provider: AcademicSearchProvider, query: str
    ) -> tuple[list[Paper], ProviderStatus]:
        """Search one provider and NEVER raise: a failure becomes an 'error' status instead.

        This is what keeps PubMed's results alive when Crossref breaks (and vice versa).
        """
        try:
            # wait_for cancels the search if it exceeds the time budget.
            papers = await asyncio.wait_for(provider.search(query), timeout=self._budget)
        except asyncio.TimeoutError:
            logger.warning("Provider %s exceeded its %ss time budget", provider.name, self._budget)
            return [], ProviderStatus(status="error", error="exceeded time budget")
        except ProviderError as exc:
            # An expected failure (timeout, HTTP error...). Its message is safe to show users.
            logger.warning("Provider %s failed: %s", provider.name, exc.message)
            return [], ProviderStatus(status="error", error=exc.message)
        except Exception:
            # A bug in one provider must not take down the others.
            # We log the details but show users only a generic message.
            logger.exception("Provider %s raised an unexpected error", provider.name)
            return [], ProviderStatus(status="error", error="unexpected error")
        return papers, ProviderStatus(status="ok", paper_count=len(papers))
