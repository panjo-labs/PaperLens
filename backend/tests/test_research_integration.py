"""POST /research through the real stack:

    FastAPI -> orchestrator -> PubMedProvider + CrossrefProvider -> parsers

Only the network is mocked (respx intercepts the real shared httpx client at the NCBI and
Crossref URLs); no provider is faked.
"""

import asyncio
import json
import time

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.main import create_app
from app.providers.crossref import WORKS_URL
from app.providers.pubmed import EFETCH_URL, ESEARCH_URL
from tests.conftest import FIXTURES

QUESTION = "What is the effectiveness of exercise therapy for chronic low back pain?"
NORMALIZED = "effectiveness exercise therapy chronic low back pain"
# Relevance order from esearch; deliberately different from the order in the efetch fixture.
RANKED_PMIDS = ["42602955", "42806217", "42813137", "39306741", "42802597"]
EMPTY_CROSSREF = {"status": "ok", "message": {"items": []}}


@pytest.fixture
def crossref_json() -> dict:
    return json.loads((FIXTURES / "crossref_sample.json").read_text(encoding="utf-8"))


def make_client(settings, **overrides) -> TestClient:
    # An API key and mailto give both real providers their faster rate limits.
    update = {"ncbi_api_key": SecretStr("test-key"), "crossref_mailto": "ops@example.org", **overrides}
    return TestClient(create_app(settings.model_copy(update=update)))


@pytest.fixture
def client(settings):
    with make_client(settings) as test_client:
        yield test_client


def esearch_body(ids):
    return {"esearchresult": {"count": str(len(ids)), "idlist": ids}}


def crossref_duplicate(doi, title):
    """A Crossref record for a paper PubMed also returned (same DOI, written differently)."""
    return {
        "DOI": doi, "title": [title], "container-title": ["Crossref Journal Name"],
        "issued": {"date-parts": [[2026, 10, 3]]}, "URL": f"https://doi.org/{doi}",
    }


@pytest.fixture
def overlapping_crossref(crossref_json) -> dict:
    """The Crossref sample plus two records that are the SAME papers as PubMed's 42806217 and
    42802597 (DOIs differ only in letter case / a "https://doi.org/" prefix). No abstracts."""
    body = json.loads(json.dumps(crossref_json))
    body["message"]["items"] += [
        crossref_duplicate("10.1002/EJP.70388", "Effectiveness, Mediators and Moderators of Remote Exercise"),
        crossref_duplicate("https://doi.org/10.1002/nbm.70415", "Predicting Clinical Improvement in Chronic Low Back Pain"),
    ]
    return body


@respx.mock
def test_question_to_combined_deduplicated_ranked_papers(client, efetch_xml, overlapping_crossref):
    esearch = respx.get(ESEARCH_URL).respond(json=esearch_body(RANKED_PMIDS))
    efetch = respx.get(EFETCH_URL).respond(content=efetch_xml)
    crossref = respx.get(WORKS_URL).respond(json=overlapping_crossref)

    response = client.post("/research", json={"question": QUESTION})

    assert response.status_code == 200
    body = response.json()
    assert body["question"] == QUESTION
    assert body["queries_used"] == [NORMALIZED]

    # both providers searched with the same normalized query and identified themselves
    pubmed_params = esearch.calls.last.request.url.params
    assert pubmed_params["term"] == NORMALIZED
    assert (pubmed_params["retmax"], pubmed_params["sort"]) == ("20", "relevance")
    assert pubmed_params["api_key"] == "test-key"
    assert efetch.calls.last.request.url.params["id"] == ",".join(RANKED_PMIDS)
    crossref_params = crossref.calls.last.request.url.params
    assert crossref_params["query.bibliographic"] == NORMALIZED
    assert crossref_params["rows"] == "20"
    assert crossref_params["mailto"] == "ops@example.org"

    # provider_status reports what each provider RETURNED (5 + 8 raw records) ...
    assert body["provider_status"] == {
        "pubmed": {"status": "ok", "paper_count": 5, "error": None},
        "crossref": {"status": "ok", "paper_count": 8, "error": None},
    }
    # ... while `papers` holds the unique ones: 13 raw - 2 duplicates = 11.
    papers = body["papers"]
    assert len(papers) == 11
    assert len({p["id"] for p in papers}) == 11

    # the two overlapping papers were merged into the PubMed record, keeping both provenances
    by_id = {p["id"]: p for p in papers}
    merged = by_id["pubmed:42806217"]
    assert merged["source_ids"] == ["pubmed:42806217", "crossref:10.1002/EJP.70388"]
    assert merged["abstract"].startswith("BACKGROUND: ")  # PubMed's abstract survived (Crossref had none)
    assert merged["doi"] == "10.1002/ejp.70388"
    assert by_id["pubmed:42802597"]["source_ids"] == [
        "pubmed:42802597", "crossref:10.1002/nbm.70415",  # the parser already removed the URL prefix
    ]
    assert "crossref:10.1002/EJP.70388" not in by_id  # no separate copy left over

    # each provider's ORIGINAL position survives dedup and ranking (ranking reordered the list):
    # PubMed positions follow esearch's relevance order (RANKED_PMIDS), Crossref's follow its response order
    assert merged["provider_ranks"] == {"pubmed": 2, "crossref": 7}  # 2nd for PubMed, 7th for Crossref
    assert by_id["pubmed:42802597"]["provider_ranks"] == {"pubmed": 5, "crossref": 8}
    assert by_id["pubmed:42602955"]["provider_ranks"] == {"pubmed": 1}
    assert by_id["crossref:10.5348/100041d05pa2018ra"]["provider_ranks"] == {"crossref": 1}
    for pmid, position in zip(RANKED_PMIDS, range(1, 6)):
        assert by_id[f"pubmed:{pmid}"]["provider_ranks"]["pubmed"] == position

    # papers are ranked: scores never increase down the list, and the top paper is a best match
    scores = [p["rank_score"] for p in papers]
    assert scores == sorted(scores, reverse=True)
    assert all(0.0 <= score <= 1.0 for score in scores)
    assert scores[0] > scores[-1]

    # every paper has the same normalized shape plus provenance and score
    assert {tuple(sorted(p)) for p in papers} == {
        (
            "abstract", "authors", "doi", "id", "journal", "provider_ranks", "publication_date",
            "rank_score", "source", "source_id", "source_ids", "title", "url",
        )
    }
    assert {p["source"] for p in papers} == {"pubmed", "crossref"}
    assert by_id["pubmed:42813137"]["doi"] is None  # missing DOI stays missing
    assert by_id["pubmed:39306741"]["abstract"] is None
    assert by_id["crossref:10.29011/2576-957x.100028"]["authors"] == []
    assert by_id["crossref:10.29011/2576-957x.100028"]["publication_date"] == "2020"
    assert "test-key" not in response.text


@respx.mock
def test_ranking_reorders_papers_by_match_instead_of_provider_order(client, efetch_xml, crossref_json):
    respx.get(ESEARCH_URL).respond(json=esearch_body(RANKED_PMIDS))
    respx.get(EFETCH_URL).respond(content=efetch_xml)
    respx.get(WORKS_URL).respond(json=crossref_json)

    papers = client.post("/research", json={"question": QUESTION}).json()["papers"]

    # Without ranking the list would be PubMed's 5 papers followed by Crossref's 6.
    assert [p["source"] for p in papers[:5]] != ["pubmed"] * 5
    # The best match is a Crossref paper whose title AND abstract repeat the question's words
    # (PubMed papers that match less well come after it).
    assert papers[0]["id"] == "crossref:10.36283/pjr.zu.14.2/004"
    assert papers[0]["rank_score"] > 0.9
    # The paper that matches the question least is last (title shares few words, no abstract).
    assert papers[-1]["rank_score"] == min(p["rank_score"] for p in papers)


@respx.mock
def test_overlapping_papers_are_merged_even_when_one_provider_returns_only_duplicates(client, efetch_xml):
    only_duplicates = {
        "status": "ok",
        "message": {"items": [crossref_duplicate("10.1002/ejp.70388", "Remote exercise")]},
    }
    respx.get(ESEARCH_URL).respond(json=esearch_body(RANKED_PMIDS))
    respx.get(EFETCH_URL).respond(content=efetch_xml)
    respx.get(WORKS_URL).respond(json=only_duplicates)

    body = client.post("/research", json={"question": QUESTION}).json()

    assert len(body["papers"]) == 5  # the single Crossref record merged into a PubMed one
    assert body["provider_status"]["crossref"]["paper_count"] == 1


@respx.mock
@pytest.mark.parametrize(
    ("failure", "expected_error"),
    [
        (httpx.Response(503), "HTTP 503 after retry"),
        (httpx.Response(429), "HTTP 429 after retry"),
        (httpx.Response(500, json={"status": "failed"}), "HTTP 500 after retry"),
        (httpx.ReadTimeout("slow"), "request timed out"),
        (httpx.ConnectError("down"), "network error"),
        (httpx.Response(200, content=b"<html>maintenance</html>"), "unexpected response"),
        (httpx.Response(200, json={"status": "failed", "message": {}}), "unexpected response"),
    ],
)
def test_pubmed_papers_survive_a_crossref_failure(client, efetch_xml, failure, expected_error):
    respx.get(ESEARCH_URL).respond(json=esearch_body(RANKED_PMIDS))
    respx.get(EFETCH_URL).respond(content=efetch_xml)
    effect = failure if isinstance(failure, Exception) else [failure, failure]
    respx.get(WORKS_URL).mock(side_effect=effect)

    response = client.post("/research", json={"question": QUESTION})

    assert response.status_code == 200
    body = response.json()
    assert [p["source"] for p in body["papers"]] == ["pubmed"] * 5
    scores = [p["rank_score"] for p in body["papers"]]
    assert scores == sorted(scores, reverse=True)  # survivors are still ranked
    assert body["provider_status"]["pubmed"]["status"] == "ok"
    assert body["provider_status"]["crossref"] == {
        "status": "error",
        "paper_count": 0,
        "error": expected_error,
    }


@respx.mock
def test_crossref_papers_survive_a_pubmed_failure(client, crossref_json):
    respx.get(ESEARCH_URL).respond(503)
    respx.get(WORKS_URL).respond(json=crossref_json)

    response = client.post("/research", json={"question": QUESTION})

    assert response.status_code == 200
    body = response.json()
    assert {p["source"] for p in body["papers"]} == {"crossref"} and len(body["papers"]) == 6
    assert body["provider_status"]["pubmed"]["error"] == "HTTP 503 after retry"
    assert body["provider_status"]["crossref"]["status"] == "ok"


@respx.mock
def test_pubmed_xml_failure_does_not_discard_crossref(client, crossref_json):
    respx.get(ESEARCH_URL).respond(json=esearch_body(["1"]))
    respx.get(EFETCH_URL).respond(content=b"<PubmedArticleSet><broken>")
    respx.get(WORKS_URL).respond(json=crossref_json)

    response = client.post("/research", json={"question": QUESTION})

    assert response.status_code == 200
    assert response.json()["provider_status"]["pubmed"]["error"] == "response could not be parsed"
    assert len(response.json()["papers"]) == 6


@respx.mock
def test_both_providers_failing_is_502_with_both_errors(client):
    respx.get(ESEARCH_URL).respond(503)
    respx.get(WORKS_URL).mock(side_effect=httpx.ReadTimeout("slow"))

    response = client.post("/research", json={"question": QUESTION})

    assert response.status_code == 502
    body = response.json()
    assert body["papers"] == []
    assert body["provider_status"]["pubmed"]["error"] == "HTTP 503 after retry"
    assert body["provider_status"]["crossref"]["error"] == "request timed out"
    assert "test-key" not in response.text


@respx.mock
def test_no_results_from_either_provider_is_a_successful_empty_response(client):
    respx.get(ESEARCH_URL).respond(json=esearch_body([]))
    efetch = respx.get(EFETCH_URL).respond(content=b"")
    respx.get(WORKS_URL).respond(json=EMPTY_CROSSREF)

    response = client.post("/research", json={"question": QUESTION})

    assert response.status_code == 200
    assert response.json()["papers"] == []
    assert response.json()["provider_status"] == {
        "pubmed": {"status": "ok", "paper_count": 0, "error": None},
        "crossref": {"status": "ok", "paper_count": 0, "error": None},
    }
    assert not efetch.called


@respx.mock
def test_providers_are_queried_concurrently(client, efetch_xml, crossref_json):
    delay = 0.4

    async def slow_esearch(request):
        await asyncio.sleep(delay)
        return httpx.Response(200, json=esearch_body(RANKED_PMIDS))

    async def slow_crossref(request):
        await asyncio.sleep(delay)
        return httpx.Response(200, json=crossref_json)

    respx.get(ESEARCH_URL).mock(side_effect=slow_esearch)
    respx.get(EFETCH_URL).respond(content=efetch_xml)
    respx.get(WORKS_URL).mock(side_effect=slow_crossref)

    start = time.monotonic()
    response = client.post("/research", json={"question": QUESTION})
    elapsed = time.monotonic() - start

    assert response.status_code == 200
    assert len(response.json()["papers"]) == 11
    # sequential execution would need >= 2 * delay (0.8 s) before PubMed's second call
    assert elapsed < 0.7, elapsed


@respx.mock
def test_a_hung_provider_is_cut_off_and_the_other_still_answers(settings, efetch_xml):
    async def hang(request):
        await asyncio.sleep(30)
        return httpx.Response(200, json=EMPTY_CROSSREF)

    respx.get(ESEARCH_URL).respond(json=esearch_body(RANKED_PMIDS))
    respx.get(EFETCH_URL).respond(content=efetch_xml)
    respx.get(WORKS_URL).mock(side_effect=hang)

    start = time.monotonic()
    with make_client(settings, provider_time_budget_seconds=0.3) as test_client:
        response = test_client.post("/research", json={"question": QUESTION})

    assert time.monotonic() - start < 3
    assert response.status_code == 200
    body = response.json()
    assert len(body["papers"]) == 5
    assert body["provider_status"]["pubmed"]["status"] == "ok"
    assert body["provider_status"]["crossref"]["error"] == "exceeded time budget"
