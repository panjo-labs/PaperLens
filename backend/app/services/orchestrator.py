import asyncio
import logging
import time
from collections.abc import Callable, Sequence

from app.providers.base import AcademicSearchProvider, ProviderError
from app.schemas.evidence import Evidence
from app.schemas.paper import Paper
from app.schemas.research import ProviderStatus, ResearchResponse
from app.services.deduplicator import deduplicate
from app.services.evidence.base import EvidenceExtractor
from app.services.query_processor import ProcessedQuery, process_question
from app.services.ranker import rank_papers

logger = logging.getLogger(__name__)


class ResearchOrchestrator:
    """Runs the research workflow:
    question -> query -> provider searches (concurrent) -> deduplicate -> rank -> evidence extraction.

    It only knows about the generic `AcademicSearchProvider` interface, never about PubMed or
    Crossref specifically. Adding a new source = adding it to the `providers` list.
    """

    def __init__(
        self,
        providers: Sequence[AcademicSearchProvider],
        query_processor: Callable[[str], ProcessedQuery] = process_question,
        provider_time_budget: float | None = None,
        deduplicator: Callable[[Sequence[Paper]], list[Paper]] = deduplicate,
        ranker: Callable[[str, Sequence[Paper]], list[Paper]] = rank_papers,
        evidence_extractor: EvidenceExtractor | None = None,
    ) -> None:
        self._providers = list(providers)
        self._process = query_processor
        # Max seconds ONE provider may take in total (None = no limit, used mainly in tests).
        self._budget = provider_time_budget
        self._deduplicate = deduplicator  # merges the same paper found by several providers
        self._rank = ranker  # orders the unique papers by relevance to the question
        self._extractor = evidence_extractor  # turns each ranked paper into structured Evidence (None = skip)

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

        # Step 3: combine. Papers are concatenated in provider order (PubMed first, so its
        # record leads when duplicates merge), and each provider gets its own entry in
        # `statuses` so the caller can see who worked. `paper_count` stays the RAW count
        # returned by that provider, before duplicates are removed.
        combined: list[Paper] = []
        statuses: dict[str, ProviderStatus] = {}
        for provider, (provider_papers, status) in zip(self._providers, outcomes):
            combined.extend(provider_papers)
            statuses[provider.name] = status

        # Step 4: merge duplicates, then Step 5: order by relevance. Both are pure in-memory
        # work (no network), so they add only a tiny delay.
        started = time.perf_counter()
        unique = self._deduplicate(combined)
        papers = self._rank(processed.search_query, unique)
        logger.info(
            "raw=%d unique=%d (removed %d duplicates); dedup+rank took %.1f ms",
            len(combined),
            len(unique),
            len(combined) - len(unique),
            (time.perf_counter() - started) * 1000,
        )

        # Step 6: structured evidence for each ranked paper (works on data we already have: no network).
        evidence = await self._extract_evidence(papers)

        return ResearchResponse(
            question=processed.original,
            queries_used=[processed.search_query],
            papers=papers,
            provider_status=statuses,
            evidence=evidence,
        )

    async def _extract_evidence(self, papers: Sequence[Paper]) -> dict[str, Evidence]:
        """Evidence per paper id, in ranked order. A failure for one paper skips only that paper."""
        if self._extractor is None:
            return {}
        started = time.perf_counter()
        results = await asyncio.gather(*(self._extract_one(paper) for paper in papers))
        evidence = {paper.id: ev for paper, ev in zip(papers, results) if ev is not None}
        logger.info("evidence for %d/%d papers in %.1f ms", len(evidence), len(papers), (time.perf_counter() - started) * 1000)
        return evidence

    async def _extract_one(self, paper: Paper) -> Evidence | None:
        try:
            evidence = await self._extractor.extract(paper)
        except Exception:
            # One paper that cannot be processed must not cost us the other papers' evidence.
            logger.exception("Evidence extraction failed for %s", paper.id)
            return None
        if evidence.paper_id != paper.id:
            # Evidence must always belong to the paper it is filed under; never attach it to the wrong one.
            logger.error("Extractor returned evidence for %s while asked about %s; discarded", evidence.paper_id, paper.id)
            return None
        return evidence

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
        # Remember where THIS provider placed each paper (1 = its best match). This is the only
        # place that sees every provider's original order, before deduplication and ranking
        # shuffle things; it is kept so a future ranker can use it (see Paper.provider_ranks).
        papers = [
            paper.model_copy(update={"provider_ranks": {provider.name: position}})
            for position, paper in enumerate(papers, 1)
        ]
        return papers, ProviderStatus(status="ok", paper_count=len(papers))
