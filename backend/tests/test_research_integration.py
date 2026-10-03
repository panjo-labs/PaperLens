"""POST /research through the real stack: FastAPI -> orchestrator -> PubMedProvider -> parser.

Only the network is mocked (respx intercepts the real httpx client at the NCBI URLs).
"""

import asyncio

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.main import create_app
from app.providers.pubmed import EFETCH_URL, ESEARCH_URL

QUESTION = "What is the effectiveness of exercise therapy for chronic low back pain?"
# Relevance order from "esearch"; deliberately different from the order in the efetch fixture.
RANKED_PMIDS = ["42602955", "42806217", "42813137", "39306741", "42802597"]


@pytest.fixture
def client(settings):
    # An API key gives the real provider's 10 req/s limiter, so the test is not slowed by 3 req/s.
    app = create_app(settings.model_copy(update={"ncbi_api_key": SecretStr("test-key")}))
    with TestClient(app) as test_client:
        yield test_client


def esearch_body(ids):
    return {"esearchresult": {"count": str(len(ids)), "idlist": ids}}


@respx.mock
def test_question_to_normalized_papers(client, efetch_xml):
    esearch = respx.get(ESEARCH_URL).respond(json=esearch_body(RANKED_PMIDS))
    efetch = respx.get(EFETCH_URL).respond(content=efetch_xml)

    response = client.post("/research", json={"question": QUESTION})

    assert response.status_code == 200
    body = response.json()

    # question -> normalized query -> esearch parameters
    expected_query = "effectiveness exercise therapy chronic low back pain"
    assert body["question"] == QUESTION
    assert body["queries_used"] == [expected_query]
    search_params = esearch.calls.last.request.url.params
    assert search_params["term"] == expected_query
    assert search_params["retmax"] == "20"
    assert search_params["sort"] == "relevance"

    # esearch IDs -> efetch request, with NCBI identification on both calls
    fetch_params = efetch.calls.last.request.url.params
    assert fetch_params["id"] == ",".join(RANKED_PMIDS)
    for params in (search_params, fetch_params):
        assert params["tool"] == "evidence-first-research-assistant"
        assert params["email"] == "test@example.com"
        assert params["api_key"] == "test-key"

    # XML -> Paper[] in relevance order, serialized to the response schema
    papers = body["papers"]
    assert [p["id"] for p in papers] == [f"pubmed:{pmid}" for pmid in RANKED_PMIDS]
    assert body["provider_status"] == {"pubmed": {"status": "ok", "paper_count": 5, "error": None}}

    by_id = {p["source_id"]: p for p in papers}
    assert "Back2Health Consortium" in by_id["42602955"]["authors"]  # collective author
    assert by_id["42806217"]["abstract"].startswith("BACKGROUND: ")  # structured abstract
    assert by_id["42806217"]["doi"] == "10.1002/ejp.70388"
    assert by_id["42806217"]["publication_date"] == "2026-10"  # month precision, no invented day
    assert by_id["42813137"]["doi"] is None  # missing DOI
    assert by_id["39306741"]["abstract"] is None  # missing abstract
    assert by_id["39306741"]["url"] == "https://pubmed.ncbi.nlm.nih.gov/39306741/"
    assert "test-key" not in response.text


@respx.mock
def test_no_matches_returns_empty_success_without_efetch(client):
    respx.get(ESEARCH_URL).respond(json=esearch_body([]))
    efetch = respx.get(EFETCH_URL).respond(content=b"")

    response = client.post("/research", json={"question": QUESTION})

    assert response.status_code == 200
    assert response.json()["papers"] == []
    assert response.json()["provider_status"]["pubmed"]["status"] == "ok"
    assert not efetch.called


@respx.mock
def test_pubmed_outage_returns_502_without_leaking_the_key(client):
    esearch = respx.get(ESEARCH_URL).respond(503)

    response = client.post("/research", json={"question": QUESTION})

    assert response.status_code == 502
    assert response.json()["papers"] == []
    assert response.json()["provider_status"]["pubmed"] == {
        "status": "error",
        "paper_count": 0,
        "error": "HTTP 503 after retry",
    }
    assert esearch.call_count == 2  # one retry
    assert "test-key" not in response.text


@respx.mock
def test_malformed_efetch_xml_is_a_provider_error(client):
    respx.get(ESEARCH_URL).respond(json=esearch_body(["1"]))
    respx.get(EFETCH_URL).respond(content=b"<PubmedArticleSet><broken>")

    response = client.post("/research", json={"question": QUESTION})

    assert response.status_code == 502
    assert response.json()["provider_status"]["pubmed"]["error"] == "response could not be parsed"


@respx.mock
def test_provider_time_budget_cuts_off_a_hung_provider(settings):
    async def hang(request):
        await asyncio.sleep(30)
        return httpx.Response(200, json=esearch_body([]))

    respx.get(ESEARCH_URL).mock(side_effect=hang)
    app = create_app(settings.model_copy(update={"provider_time_budget_seconds": 0.2}))

    with TestClient(app) as test_client:
        response = test_client.post("/research", json={"question": QUESTION})

    assert response.status_code == 502
    assert response.json()["provider_status"]["pubmed"]["error"] == "exceeded time budget"
