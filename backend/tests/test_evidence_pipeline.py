"""Evidence extraction inside the pipeline:

    POST /research -> query -> PubMed + Crossref -> deduplicate -> rank -> evidence extraction -> response
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
from app.providers.base import ProviderError
from app.providers.crossref import WORKS_URL
from app.providers.pubmed import EFETCH_URL, ESEARCH_URL
from app.schemas.evidence import Evidence, FieldStatus
from app.schemas.paper import Paper
from app.services.evidence import RuleBasedEvidenceExtractor
from app.services.orchestrator import ResearchOrchestrator
from tests.conftest import FIXTURES
from tests.test_evidence_extractor import assert_traceable

QUESTION = "What is the effectiveness of exercise therapy for chronic low back pain?"
RANKED_PMIDS = ["42602955", "42806217", "42813137", "39306741", "42802597"]


def paper(source, source_id, title="Yoga for back pain", doi=None, abstract=None) -> Paper:
    return Paper(id=f"{source}:{source_id}", title=title, authors=[], doi=doi, abstract=abstract, source=source, source_id=source_id)


class StubExtractor:
    """A minimal EvidenceExtractor for orchestrator tests: records what it was asked, can fail or misbehave."""

    name = "stub"

    def __init__(self, fail_on=(), wrong_id_for=(), delay=0.0):
        self.seen: list[str] = []
        self._fail_on, self._wrong_id_for, self._delay = set(fail_on), set(wrong_id_for), delay

    async def extract(self, paper: Paper) -> Evidence:
        self.seen.append(paper.id)
        await asyncio.sleep(self._delay)
        if paper.id in self._fail_on:
            raise RuntimeError("boom: internal detail")
        pid = "pubmed:999999" if paper.id in self._wrong_id_for else paper.id
        return Evidence(paper_id=pid, extractor=self.name, abstract_status="present" if paper.abstract else "missing")


# --- orchestrator ---------------------------------------------------------------------------------------


async def test_evidence_is_keyed_by_paper_id_in_ranked_order(fake_provider):
    provider = fake_provider("pubmed", [paper("pubmed", "1", "Unrelated cats"), paper("pubmed", "2", "Yoga for chronic back pain")])

    result = await ResearchOrchestrator([provider], evidence_extractor=StubExtractor()).research("yoga for back pain")

    assert [p.id for p in result.papers] == ["pubmed:2", "pubmed:1"]  # ranking put the better match first
    assert list(result.evidence) == ["pubmed:2", "pubmed:1"]  # evidence follows the same order
    assert all(key == ev.paper_id for key, ev in result.evidence.items())


async def test_without_an_extractor_the_response_has_empty_evidence_and_is_otherwise_unchanged(fake_provider):
    result = await ResearchOrchestrator([fake_provider("pubmed", [paper("pubmed", "1")])]).research("yoga for back pain")

    assert result.evidence == {}
    assert len(result.papers) == 1 and result.provider_status["pubmed"].status == "ok"


async def test_extractor_runs_after_deduplication_and_ranking_on_unique_papers_only(fake_provider):
    stub = StubExtractor()
    pm = fake_provider("pubmed", [paper("pubmed", "1", "Other"), paper("pubmed", "2", "Yoga back pain", doi="10.1/y")])
    cr = fake_provider("crossref", [paper("crossref", "10.1/Y", "Yoga back pain", doi="10.1/Y")])

    result = await ResearchOrchestrator([pm, cr], evidence_extractor=stub).research("yoga back pain")

    assert sorted(stub.seen) == ["pubmed:1", "pubmed:2"]  # 3 raw records, 2 unique papers: the duplicate is not extracted twice
    assert "crossref:10.1/Y" not in result.evidence  # the merged duplicate has no evidence of its own
    assert result.evidence["pubmed:2"].paper_id == "pubmed:2"  # filed under the canonical id


async def test_one_failing_extraction_costs_only_that_paper(fake_provider):
    provider = fake_provider("pubmed", [paper("pubmed", str(n)) for n in (1, 2, 3)])

    result = await ResearchOrchestrator([provider], evidence_extractor=StubExtractor(fail_on=["pubmed:2"])).research("yoga for back pain")

    assert len(result.papers) == 3  # retrieval results are untouched
    assert sorted(result.evidence) == ["pubmed:1", "pubmed:3"]  # the failed paper has no evidence: not a made-up one
    assert result.provider_status["pubmed"].status == "ok"


async def test_evidence_filed_under_the_wrong_paper_id_is_discarded(fake_provider):
    provider = fake_provider("pubmed", [paper("pubmed", "1"), paper("pubmed", "2")])

    result = await ResearchOrchestrator([provider], evidence_extractor=StubExtractor(wrong_id_for=["pubmed:2"])).research("yoga for back pain")

    assert list(result.evidence) == ["pubmed:1"]
    assert all(key == ev.paper_id for key, ev in result.evidence.items())


async def test_evidence_still_comes_from_the_surviving_provider(fake_provider):
    ok = fake_provider("pubmed", [paper("pubmed", "1")])
    bad = fake_provider("crossref", error=ProviderError("crossref", "HTTP 503 after retry"))

    result = await ResearchOrchestrator([ok, bad], evidence_extractor=StubExtractor()).research("yoga for back pain")

    assert list(result.evidence) == ["pubmed:1"]
    assert result.provider_status["crossref"].status == "error"


async def test_no_papers_means_no_evidence(fake_provider):
    result = await ResearchOrchestrator([fake_provider("pubmed")], evidence_extractor=StubExtractor()).research("yoga for back pain")
    assert result.papers == [] and result.evidence == {}


async def test_papers_are_extracted_concurrently(fake_provider):
    provider = fake_provider("pubmed", [paper("pubmed", str(n)) for n in range(6)])
    start = time.perf_counter()
    await ResearchOrchestrator([provider], evidence_extractor=StubExtractor(delay=0.15)).research("yoga for back pain")
    assert time.perf_counter() - start < 0.6  # six sequential 0.15 s extractions would need 0.9 s


async def test_real_extractor_works_on_real_looking_papers_from_the_orchestrator(fake_provider):
    p = paper("pubmed", "1", "Yoga for adults with back pain: a randomized controlled trial",
              abstract="METHODS: 40 adults with chronic back pain were randomly allocated to yoga or usual care.\nRESULTS: Pain fell (p < 0.01).")
    result = await ResearchOrchestrator([fake_provider("pubmed", [p])], evidence_extractor=RuleBasedEvidenceExtractor()).research("yoga back pain")

    ev = result.evidence["pubmed:1"]
    assert ev.study_design.value == "randomized controlled trial" and ev.sample_size.participants == 40
    assert ev.findings.items[0].statistics == ["p < 0.01"]
    assert_traceable(p, ev)


# --- API (real providers, only the network mocked) ------------------------------------------------------


@pytest.fixture
def crossref_json() -> dict:
    return json.loads((FIXTURES / "crossref_sample.json").read_text(encoding="utf-8"))


def api_client(settings, **overrides) -> TestClient:
    update = {"ncbi_api_key": SecretStr("test-key"), "crossref_mailto": "ops@example.org", **overrides}
    return TestClient(create_app(settings.model_copy(update=update)))


def esearch_body(ids):
    return {"esearchresult": {"count": str(len(ids)), "idlist": ids}}


@respx.mock
def test_post_research_returns_traceable_evidence_for_every_ranked_paper(settings, efetch_xml, crossref_json):
    respx.get(ESEARCH_URL).respond(json=esearch_body(RANKED_PMIDS))
    respx.get(EFETCH_URL).respond(content=efetch_xml)
    respx.get(WORKS_URL).respond(json=crossref_json)

    with api_client(settings) as client:
        response = client.post("/research", json={"question": QUESTION})

    assert response.status_code == 200
    body = response.json()
    papers = [Paper(**p) for p in body["papers"]]
    evidence = {key: Evidence.model_validate(value) for key, value in body["evidence"].items()}

    assert len(papers) == 11
    assert list(evidence) == [p.id for p in papers]  # one Evidence per paper, same (ranked) order
    for p in papers:
        assert evidence[p.id].paper_id == p.id
        assert_traceable(p, evidence[p.id])  # every value really is in the text it cites


@respx.mock
def test_the_response_keeps_every_existing_key_and_only_adds_evidence(settings, efetch_xml, crossref_json):
    respx.get(ESEARCH_URL).respond(json=esearch_body(RANKED_PMIDS))
    respx.get(EFETCH_URL).respond(content=efetch_xml)
    respx.get(WORKS_URL).respond(json=crossref_json)

    with api_client(settings) as client:
        body = client.post("/research", json={"question": QUESTION}).json()

    assert set(body) == {"question", "queries_used", "papers", "provider_status", "evidence"}
    assert body["provider_status"] == {
        "pubmed": {"status": "ok", "paper_count": 5, "error": None},
        "crossref": {"status": "ok", "paper_count": 6, "error": None},
    }
    assert "evidence" not in body["papers"][0]  # evidence is NOT embedded in Paper
    assert "test-key" not in json.dumps(body)


@respx.mock
def test_a_paper_without_an_abstract_has_missing_status_and_no_invented_fields(settings, efetch_xml, crossref_json):
    respx.get(ESEARCH_URL).respond(json=esearch_body(RANKED_PMIDS))
    respx.get(EFETCH_URL).respond(content=efetch_xml)
    respx.get(WORKS_URL).respond(json=crossref_json)

    with api_client(settings) as client:
        body = client.post("/research", json={"question": QUESTION}).json()

    no_abstract = Evidence.model_validate(body["evidence"]["pubmed:39306741"])  # a PubMed record with no <Abstract>
    assert no_abstract.abstract_status == "missing"
    for name in ("sample_size", "intervention", "comparator", "outcomes", "findings", "limitations"):
        assert getattr(no_abstract, name).status is FieldStatus.UNAVAILABLE, name

    crossref_no_abstract = Evidence.model_validate(body["evidence"]["crossref:10.29011/2576-957x.100028"])
    assert crossref_no_abstract.abstract_status == "missing"


@respx.mock
def test_a_structured_pubmed_abstract_keeps_its_section_labels_in_the_evidence(settings, efetch_xml, crossref_json):
    respx.get(ESEARCH_URL).respond(json=esearch_body(RANKED_PMIDS))
    respx.get(EFETCH_URL).respond(content=efetch_xml)
    respx.get(WORKS_URL).respond(json=crossref_json)

    with api_client(settings) as client:
        body = client.post("/research", json={"question": QUESTION}).json()

    ev = Evidence.model_validate(body["evidence"]["pubmed:42806217"])  # abstract labelled BACKGROUND / METHODS / RESULTS / ...
    labels = [f.source.section for f in ev.findings.items]

    assert labels and all(labels), "every finding must say which abstract section it came from"
    assert any("RESULT" in label.upper() for label in labels)
    assert ev.sample_size.status is FieldStatus.UNAVAILABLE or ev.sample_size.sources[0].section is not None


@respx.mock
def test_merged_duplicates_get_one_evidence_under_the_canonical_id(settings, efetch_xml, crossref_json):
    crossref_json["message"]["items"].append(
        {"DOI": "10.1002/EJP.70388", "title": ["Effectiveness, Mediators and Moderators of Remote Exercise"],
         "issued": {"date-parts": [[2026, 10, 3]]}}
    )
    respx.get(ESEARCH_URL).respond(json=esearch_body(RANKED_PMIDS))
    respx.get(EFETCH_URL).respond(content=efetch_xml)
    respx.get(WORKS_URL).respond(json=crossref_json)

    with api_client(settings) as client:
        body = client.post("/research", json={"question": QUESTION}).json()

    assert len(body["papers"]) == 11 and len(body["evidence"]) == 11  # 12 raw records - 1 duplicate
    assert "pubmed:42806217" in body["evidence"]
    assert "crossref:10.1002/EJP.70388" not in body["evidence"]
    merged = Evidence.model_validate(body["evidence"]["pubmed:42806217"])
    assert merged.abstract_status == "present"  # PubMed's abstract was kept when the records merged


@respx.mock
def test_one_provider_failing_still_yields_evidence_for_the_other(settings, efetch_xml):
    respx.get(ESEARCH_URL).respond(json=esearch_body(RANKED_PMIDS))
    respx.get(EFETCH_URL).respond(content=efetch_xml)
    respx.get(WORKS_URL).respond(503)

    with api_client(settings) as client:
        response = client.post("/research", json={"question": QUESTION})

    body = response.json()
    assert response.status_code == 200 and body["provider_status"]["crossref"]["status"] == "error"
    assert len(body["evidence"]) == 5 == len(body["papers"])


@respx.mock
def test_when_every_provider_fails_the_502_has_empty_evidence(settings):
    respx.get(ESEARCH_URL).respond(503)
    respx.get(WORKS_URL).mock(side_effect=httpx.ReadTimeout("slow"))

    with api_client(settings) as client:
        response = client.post("/research", json={"question": QUESTION})

    assert response.status_code == 502
    assert response.json()["evidence"] == {} and response.json()["papers"] == []


@respx.mock
def test_zero_results_gives_an_empty_evidence_collection(settings):
    respx.get(ESEARCH_URL).respond(json=esearch_body([]))
    respx.get(WORKS_URL).respond(json={"status": "ok", "message": {"items": []}})

    with api_client(settings) as client:
        response = client.post("/research", json={"question": QUESTION})

    assert response.status_code == 200 and response.json()["evidence"] == {}


def test_evidence_extraction_adds_only_milliseconds(efetch_xml, crossref_json):
    from app.providers.crossref_parser import parse_crossref_response
    from app.providers.pubmed_parser import parse_pubmed_xml

    papers = parse_pubmed_xml(efetch_xml) + parse_crossref_response(crossref_json)  # 11 real records
    extractor = RuleBasedEvidenceExtractor()

    start = time.perf_counter()
    for p in papers:
        extractor.extract_sync(p)
    elapsed = time.perf_counter() - start

    assert len(papers) == 11
    assert elapsed < 0.5, f"{elapsed * 1000:.0f} ms for 11 papers"  # a few ms in practice; this only catches blow-ups
