import asyncio
import time

import pytest

from app.providers.base import ProviderError
from app.services.orchestrator import ResearchOrchestrator
from app.services.query_processor import InvalidQueryError


async def test_returns_papers_and_status(fake_provider, make_paper):
    provider = fake_provider("pubmed", [make_paper("pubmed", "1"), make_paper("pubmed", "2")])
    result = await ResearchOrchestrator([provider]).research(
        "What is the effect of yoga on chronic low back pain?"
    )

    assert result.question == "What is the effect of yoga on chronic low back pain?"
    assert result.queries_used == ["effect yoga chronic low back pain"]
    assert provider.queries == result.queries_used
    assert [p.id for p in result.papers] == ["pubmed:1", "pubmed:2"]
    assert result.provider_status["pubmed"].status == "ok"
    assert result.provider_status["pubmed"].paper_count == 2


async def test_provider_error_is_reported_not_raised(fake_provider):
    provider = fake_provider("pubmed", error=ProviderError("pubmed", "request timed out"))
    result = await ResearchOrchestrator([provider]).research("yoga for back pain")

    assert result.papers == []
    status = result.provider_status["pubmed"]
    assert (status.status, status.error, status.paper_count) == ("error", "request timed out", 0)


async def test_one_failing_provider_does_not_block_another(fake_provider, make_paper):
    ok = fake_provider("a", [make_paper("a", "1")])
    bad = fake_provider("b", error=ProviderError("b", "HTTP 503 after retry"))
    result = await ResearchOrchestrator([bad, ok]).research("yoga for back pain")

    assert [p.id for p in result.papers] == ["a:1"]
    assert result.provider_status["a"].status == "ok"
    assert result.provider_status["b"].status == "error"


async def test_unexpected_exception_is_contained_and_not_leaked(fake_provider):
    bad = fake_provider("a", error=RuntimeError("internal detail with secret"))
    result = await ResearchOrchestrator([bad]).research("yoga for back pain")

    assert result.provider_status["a"].error == "unexpected error"


async def test_providers_run_concurrently(fake_provider):
    providers = [fake_provider("a", delay=0.2), fake_provider("b", delay=0.2)]
    start = time.monotonic()
    await ResearchOrchestrator(providers).research("yoga for back pain")
    assert time.monotonic() - start < 0.35


async def test_question_without_search_terms_raises_before_any_provider_call(fake_provider):
    provider = fake_provider("pubmed")
    with pytest.raises(InvalidQueryError):
        await ResearchOrchestrator([provider]).research("What is it?")
    assert provider.queries == []


async def test_no_providers_gives_empty_result():
    result = await ResearchOrchestrator([]).research("yoga for back pain")
    assert result.papers == [] and result.provider_status == {}


async def test_provider_exceeding_time_budget_is_reported_and_others_survive(fake_provider, make_paper):
    slow = fake_provider("slow", delay=5)
    fast = fake_provider("fast", [make_paper("fast", "1")])
    orchestrator = ResearchOrchestrator([slow, fast], provider_time_budget=0.1)

    start = time.monotonic()
    result = await orchestrator.research("yoga for back pain")

    assert time.monotonic() - start < 1
    assert result.provider_status["slow"].status == "error"
    assert result.provider_status["slow"].error == "exceeded time budget"
    assert result.provider_status["fast"].status == "ok"
    assert [p.id for p in result.papers] == ["fast:1"]


async def test_no_budget_means_no_cutoff(fake_provider):
    result = await ResearchOrchestrator([fake_provider("a", delay=0.15)]).research("yoga for back pain")
    assert result.provider_status["a"].status == "ok"
