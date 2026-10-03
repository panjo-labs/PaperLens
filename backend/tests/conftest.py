import asyncio
from pathlib import Path

import pytest

from app.config import Settings
from app.schemas.paper import Paper

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def efetch_xml() -> bytes:
    """Real PubMed efetch response (5 records) recorded 2026-10-03; see tests/fixtures/README.md."""
    return (FIXTURES / "efetch_sample.xml").read_bytes()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        ncbi_email="test@example.com",
        ncbi_api_key=None,
        retry_backoff_seconds=0,
    )


class FakeProvider:
    """Stand-in AcademicSearchProvider: returns canned papers, raises `error`, or sleeps `delay`."""

    def __init__(self, name, papers=None, error=None, delay=0.0):
        self.name = name
        self._papers = papers or []
        self._error = error
        self._delay = delay
        self.queries: list[str] = []

    async def search(self, query):
        self.queries.append(query)
        await asyncio.sleep(self._delay)
        if self._error:
            raise self._error
        return self._papers


@pytest.fixture
def fake_provider():
    return FakeProvider


@pytest.fixture
def make_paper():
    def factory(source: str = "pubmed", source_id: str = "1") -> Paper:
        return Paper(
            id=f"{source}:{source_id}",
            title=f"Paper {source_id}",
            authors=[],
            source=source,
            source_id=source_id,
        )

    return factory
