import json

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.config import Settings
from app.providers.base import ProviderError
from app.providers.pubmed import EFETCH_URL, ESEARCH_URL, PubMedProvider
from app.providers.rate_limit import RateLimiter

FIXTURE_PMIDS = ["42602955", "42806217", "42813137", "39306741", "42802597"]


def esearch_json(ids):
    return {"header": {"type": "esearch"}, "esearchresult": {"count": str(len(ids)), "idlist": ids}}


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as http:
        yield http


@pytest.fixture
def make_provider(client, settings):
    def factory(**overrides) -> PubMedProvider:
        cfg = settings.model_copy(update=overrides) if overrides else settings
        return PubMedProvider(client, cfg, rate_limiter=RateLimiter(1000))

    return factory


@pytest.fixture
def provider(make_provider):
    return make_provider()


@respx.mock
async def test_search_returns_normalized_papers_in_relevance_order(provider, efetch_xml):
    # efetch returns records in a different order than esearch ranked them.
    respx.get(ESEARCH_URL).respond(json=esearch_json(FIXTURE_PMIDS))
    respx.get(EFETCH_URL).respond(content=efetch_xml)

    papers = await provider.search("exercise therapy low back pain")

    assert [p.source_id for p in papers] == FIXTURE_PMIDS
    assert all(p.source == "pubmed" and p.id == f"pubmed:{p.source_id}" for p in papers)


@respx.mock
async def test_esearch_request_parameters(provider):
    route = respx.get(ESEARCH_URL).respond(json=esearch_json([]))

    await provider.search("low back pain")

    params = route.calls.last.request.url.params
    assert params["db"] == "pubmed"
    assert params["term"] == "low back pain"
    assert params["retmax"] == "20"
    assert params["retmode"] == "json"
    assert params["sort"] == "relevance"
    assert params["tool"] == "evidence-first-research-assistant"
    assert params["email"] == "test@example.com"
    assert "api_key" not in params


@respx.mock
async def test_efetch_request_parameters_and_api_key(make_provider, efetch_xml):
    provider = make_provider(ncbi_api_key=SecretStr("secret-key"))
    respx.get(ESEARCH_URL).respond(json=esearch_json(["1", "2"]))
    efetch = respx.get(EFETCH_URL).respond(content=efetch_xml)

    await provider.search("x")

    params = efetch.calls.last.request.url.params
    assert params["id"] == "1,2"
    assert params["retmode"] == "xml"
    assert params["api_key"] == "secret-key"
    assert params["tool"] == "evidence-first-research-assistant"
    assert params["email"] == "test@example.com"


@respx.mock
async def test_empty_search_skips_efetch(provider):
    respx.get(ESEARCH_URL).respond(json=esearch_json([]))
    efetch = respx.get(EFETCH_URL).respond(content=b"")

    assert await provider.search("zzzz") == []
    assert not efetch.called


@respx.mock
async def test_ids_missing_from_efetch_are_dropped(provider, efetch_xml):
    respx.get(ESEARCH_URL).respond(json=esearch_json(["42806217", "99999999"]))
    respx.get(EFETCH_URL).respond(content=efetch_xml)

    assert [p.source_id for p in await provider.search("x")] == ["42806217"]


@respx.mock
@pytest.mark.parametrize("status", [429, 503])
async def test_retries_once_then_succeeds(provider, status):
    route = respx.get(ESEARCH_URL).mock(
        side_effect=[httpx.Response(status), httpx.Response(200, json=esearch_json([]))]
    )

    assert await provider.search("x") == []
    assert route.call_count == 2


@respx.mock
@pytest.mark.parametrize("status", [429, 500])
async def test_gives_up_after_one_retry(provider, status):
    route = respx.get(ESEARCH_URL).respond(status)

    with pytest.raises(ProviderError, match=f"HTTP {status}"):
        await provider.search("x")
    assert route.call_count == 2


@respx.mock
async def test_client_errors_are_not_retried(provider):
    route = respx.get(ESEARCH_URL).respond(400)

    with pytest.raises(ProviderError, match="HTTP 400"):
        await provider.search("x")
    assert route.call_count == 1


@respx.mock
async def test_efetch_failure_after_successful_search(provider):
    respx.get(ESEARCH_URL).respond(json=esearch_json(["1"]))
    respx.get(EFETCH_URL).respond(502)

    with pytest.raises(ProviderError, match="HTTP 502"):
        await provider.search("x")


@respx.mock
async def test_timeout_becomes_provider_error_without_retry(provider):
    route = respx.get(ESEARCH_URL).mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(ProviderError, match="timed out"):
        await provider.search("x")
    assert route.call_count == 1


@respx.mock
async def test_network_error_becomes_provider_error(provider):
    respx.get(ESEARCH_URL).mock(side_effect=httpx.ConnectError("down"))

    with pytest.raises(ProviderError, match="network error"):
        await provider.search("x")


@respx.mock
@pytest.mark.parametrize(
    "body",
    [
        httpx.Response(200, content=b"<html>not json</html>"),
        httpx.Response(200, json={"unexpected": True}),
        httpx.Response(200, json={"esearchresult": {"idlist": "oops"}}),
    ],
)
async def test_unexpected_search_payload(provider, body):
    respx.get(ESEARCH_URL).mock(return_value=body)

    with pytest.raises(ProviderError, match="unexpected search response"):
        await provider.search("x")


@respx.mock
async def test_esearch_error_payload(provider):
    respx.get(ESEARCH_URL).respond(
        json={"esearchresult": {"ERROR": "Invalid query", "idlist": []}}
    )

    with pytest.raises(ProviderError, match="rejected"):
        await provider.search("x")


@respx.mock
async def test_unparseable_efetch_xml(provider):
    respx.get(ESEARCH_URL).respond(json=esearch_json(["1"]))
    respx.get(EFETCH_URL).respond(content=b"<PubmedArticleSet><broken>")

    with pytest.raises(ProviderError, match="could not be parsed"):
        await provider.search("x")


@respx.mock
async def test_api_key_never_appears_in_error_messages(make_provider):
    provider = make_provider(ncbi_api_key=SecretStr("secret-key"))
    for effect in (httpx.ReadTimeout("t"), httpx.ConnectError("c"), httpx.Response(500)):
        respx.get(ESEARCH_URL).mock(side_effect=[effect, effect])
        with pytest.raises(ProviderError) as info:
            await provider.search("x")
        assert "secret-key" not in str(info.value)
        assert info.value.__cause__ is None


def test_settings_blank_api_key_means_no_key():
    cfg = Settings(_env_file=None, ncbi_email="a@b.co", ncbi_api_key="  ")
    assert cfg.ncbi_api_key is None
    assert cfg.pubmed_requests_per_second == 3
    assert Settings(_env_file=None, ncbi_email="a@b.co", ncbi_api_key="k").pubmed_requests_per_second == 10


def test_esearch_sample_fixture_shape_matches_what_we_read(settings):
    from tests.conftest import FIXTURES

    data = json.loads((FIXTURES / "esearch_sample.json").read_text())
    assert isinstance(data["esearchresult"]["idlist"], list)


# --- Retry-After handling (429 only) ----------------------------------------------------------


@pytest.fixture
def slept(monkeypatch):
    """Record backoff delays without waiting. Only the retry sleep is patched, not the rate limiter's."""
    delays: list[float] = []

    async def fake_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr("app.providers.pubmed._sleep", fake_sleep)
    return delays


async def _retry_after_delay(provider, response):
    with respx.mock:
        respx.get(ESEARCH_URL).mock(
            side_effect=[response, httpx.Response(200, json=esearch_json([]))]
        )
        await provider.search("x")


async def test_429_retry_after_seconds_is_honoured(make_provider, slept):
    await _retry_after_delay(make_provider(), httpx.Response(429, headers={"Retry-After": "2"}))
    assert slept == [2.0]


async def test_429_retry_after_is_capped(make_provider, slept):
    await _retry_after_delay(make_provider(), httpx.Response(429, headers={"Retry-After": "3600"}))
    assert slept == [5.0]


@pytest.mark.parametrize("value", ["soon", "Wed, 21 Oct 2026 07:28:00 GMT", "-3", "1.5", ""])
async def test_429_unusable_retry_after_falls_back_to_backoff(make_provider, slept, value):
    provider = make_provider(retry_backoff_seconds=0.5)
    await _retry_after_delay(provider, httpx.Response(429, headers={"Retry-After": value}))
    assert slept == [0.5]


async def test_429_retry_after_never_shorter_than_backoff(make_provider, slept):
    provider = make_provider(retry_backoff_seconds=3)
    await _retry_after_delay(provider, httpx.Response(429, headers={"Retry-After": "1"}))
    assert slept == [3.0]


async def test_5xx_ignores_retry_after(make_provider, slept):
    provider = make_provider(retry_backoff_seconds=0.5)
    await _retry_after_delay(provider, httpx.Response(503, headers={"Retry-After": "4"}))
    assert slept == [0.5]


# --- settings bounds --------------------------------------------------------------------------


@pytest.mark.parametrize("retmax", [0, -1, 101])
def test_retmax_out_of_bounds_is_rejected(retmax):
    with pytest.raises(ValueError):
        Settings(_env_file=None, ncbi_email="a@b.co", pubmed_retmax=retmax)


@pytest.mark.parametrize("retmax", [1, 20, 100])
def test_retmax_within_bounds_is_accepted(retmax):
    assert Settings(_env_file=None, ncbi_email="a@b.co", pubmed_retmax=retmax).pubmed_retmax == retmax


def test_retmax_is_read_from_environment(monkeypatch):
    monkeypatch.setenv("NCBI_EMAIL", "a@b.co")
    monkeypatch.setenv("PUBMED_RETMAX", "500")
    with pytest.raises(ValueError):
        Settings(_env_file=None)


@pytest.mark.parametrize("budget", [0, -5, 61])
def test_time_budget_out_of_bounds_is_rejected(budget):
    with pytest.raises(ValueError):
        Settings(_env_file=None, ncbi_email="a@b.co", provider_time_budget_seconds=budget)


def test_default_budget_is_between_15_and_20_seconds():
    budget = Settings(_env_file=None, ncbi_email="a@b.co").provider_time_budget_seconds
    assert 15 <= budget <= 20
