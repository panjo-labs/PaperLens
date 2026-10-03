import pytest
from fastapi.testclient import TestClient

from app.api.research import get_orchestrator
from app.main import create_app
from app.providers.base import ProviderError
from app.schemas.paper import Paper
from app.services.orchestrator import ResearchOrchestrator


@pytest.fixture
def make_client(settings):
    clients: list[TestClient] = []

    def factory(*providers) -> TestClient:
        app = create_app(settings)
        app.dependency_overrides[get_orchestrator] = lambda: ResearchOrchestrator(providers)
        client = TestClient(app)
        clients.append(client)
        return client

    yield factory


def paper() -> Paper:
    return Paper(
        id="pubmed:1",
        title="T",
        authors=["A B"],
        abstract=None,
        publication_date="2024-03",
        source="pubmed",
        source_id="1",
        url="https://pubmed.ncbi.nlm.nih.gov/1/",
    )


def test_health(make_client):
    with make_client() as client:
        assert client.get("/health").json() == {"status": "ok"}


def test_research_success_shape(make_client, fake_provider):
    with make_client(fake_provider("pubmed", [paper()])) as client:
        response = client.post("/research", json={"question": "Does yoga help back pain?"})

    assert response.status_code == 200
    body = response.json()
    assert body["question"] == "Does yoga help back pain?"
    assert body["queries_used"] == ["yoga help back pain"]
    assert body["provider_status"] == {"pubmed": {"status": "ok", "paper_count": 1, "error": None}}
    assert body["papers"][0] == {
        "id": "pubmed:1",
        "title": "T",
        "authors": ["A B"],
        "abstract": None,
        "journal": None,
        "publication_date": "2024-03",
        "doi": None,
        "source": "pubmed",
        "source_id": "1",
        "url": "https://pubmed.ncbi.nlm.nih.gov/1/",
        "source_ids": ["pubmed:1"],  # provenance: filled in automatically
        "rank_score": 0.0,  # title "T" shares no words with the question
    }


def test_no_results_is_still_success(make_client, fake_provider):
    with make_client(fake_provider("pubmed", [])) as client:
        response = client.post("/research", json={"question": "Does yoga help back pain?"})

    assert response.status_code == 200
    assert response.json()["papers"] == []


def test_all_providers_failing_returns_502_with_details(make_client, fake_provider):
    failing = fake_provider("pubmed", error=ProviderError("pubmed", "request timed out"))
    with make_client(failing) as client:
        response = client.post("/research", json={"question": "Does yoga help back pain?"})

    assert response.status_code == 502
    assert response.json()["provider_status"]["pubmed"] == {
        "status": "error",
        "paper_count": 0,
        "error": "request timed out",
    }


def test_partial_provider_failure_is_still_200(make_client, fake_provider):
    providers = [
        fake_provider("pubmed", [paper()]),
        fake_provider("other", error=ProviderError("other", "boom")),
    ]
    with make_client(*providers) as client:
        response = client.post("/research", json={"question": "Does yoga help back pain?"})

    assert response.status_code == 200
    assert response.json()["provider_status"]["other"]["status"] == "error"


@pytest.mark.parametrize(
    "payload",
    [{}, {"question": ""}, {"question": "  a "}, {"question": "x" * 1001}, {"question": 5}],
)
def test_invalid_request_bodies_are_422(make_client, fake_provider, payload):
    with make_client(fake_provider("pubmed")) as client:
        assert client.post("/research", json=payload).status_code == 422


def test_question_with_no_search_terms_is_422(make_client, fake_provider):
    provider = fake_provider("pubmed")
    with make_client(provider) as client:
        response = client.post("/research", json={"question": "What is it?"})

    assert response.status_code == 422
    assert provider.queries == []
