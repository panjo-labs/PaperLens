import json

import httpx
import pytest
import respx

from app.config import Settings
from app.providers.base import ProviderError
from app.providers.crossref import WORKS_URL, CrossrefProvider
from app.providers.rate_limit import RateLimiter
from tests.conftest import FIXTURES


@pytest.fixture
def sample_json() -> dict:
    return json.loads((FIXTURES / "crossref_sample.json").read_text(encoding="utf-8"))


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as http:
        yield http


@pytest.fixture
def make_provider(client, settings):
    def factory(**overrides) -> CrossrefProvider:
        cfg = settings.model_copy(update=overrides) if overrides else settings
        return CrossrefProvider(client, cfg, rate_limiter=RateLimiter(1000))

    return factory


@pytest.fixture
def provider(make_provider):
    return make_provider()


@respx.mock
async def test_search_returns_normalized_papers_in_crossref_order(provider, sample_json):
    respx.get(WORKS_URL).respond(json=sample_json)

    papers = await provider.search("exercise therapy low back pain")

    assert [p.doi for p in papers] == [i["DOI"] for i in sample_json["message"]["items"]]
    assert {p.source for p in papers} == {"crossref"}


@respx.mock
async def test_request_parameters(provider):
    route = respx.get(WORKS_URL).respond(json={"status": "ok", "message": {"items": []}})

    await provider.search("low back pain")

    params = route.calls.last.request.url.params
    assert params["query.bibliographic"] == "low back pain"
    assert params["rows"] == "20"
    assert params["filter"] == "type:journal-article"
    assert "abstract" in params["select"] and "DOI" in params["select"]
    assert "mailto" not in params  # no contact configured -> public pool, nothing invented


@respx.mock
async def test_mailto_is_sent_only_when_configured(make_provider):
    route = respx.get(WORKS_URL).respond(json={"status": "ok", "message": {"items": []}})

    await make_provider(crossref_mailto="ops@example.org", crossref_rows=7).search("x")

    params = route.calls.last.request.url.params
    assert params["mailto"] == "ops@example.org"
    assert params["rows"] == "7"


@respx.mock
async def test_no_results_is_an_empty_list(provider):
    respx.get(WORKS_URL).respond(json={"status": "ok", "message": {"items": []}})
    assert await provider.search("zzzz") == []


@respx.mock
async def test_unusable_records_are_dropped_good_ones_kept(provider):
    items = [None, {"title": ["no doi"]}, {"DOI": "10.1/ok", "title": ["Fine"]}]
    respx.get(WORKS_URL).respond(json={"status": "ok", "message": {"items": items}})

    assert [p.doi for p in await provider.search("x")] == ["10.1/ok"]


# --- failures -------------------------------------------------------------------------------


@respx.mock
async def test_timeout_becomes_provider_error_without_retry(provider):
    route = respx.get(WORKS_URL).mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(ProviderError, match="timed out") as info:
        await provider.search("x")
    assert info.value.provider == "crossref"
    assert route.call_count == 1


@respx.mock
async def test_connect_timeout_and_network_error(provider):
    respx.get(WORKS_URL).mock(side_effect=httpx.ConnectTimeout("t"))
    with pytest.raises(ProviderError, match="timed out"):
        await provider.search("x")

    respx.get(WORKS_URL).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(ProviderError, match="network error"):
        await provider.search("x")


@respx.mock
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_retries_once_then_gives_up(provider, status):
    route = respx.get(WORKS_URL).respond(status)

    with pytest.raises(ProviderError, match=f"HTTP {status} after retry"):
        await provider.search("x")
    assert route.call_count == 2


@respx.mock
async def test_rate_limited_then_success(provider, sample_json):
    route = respx.get(WORKS_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(200, json=sample_json),
        ]
    )

    assert len(await provider.search("x")) == 6
    assert route.call_count == 2


@respx.mock
@pytest.mark.parametrize("status", [400, 403, 404])
async def test_client_errors_are_not_retried(provider, status):
    route = respx.get(WORKS_URL).respond(status)

    with pytest.raises(ProviderError, match=f"HTTP {status}"):
        await provider.search("x")
    assert route.call_count == 1


@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"<html>Service unavailable</html>"),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, json={"status": "failed", "message": {"items": []}}),
        httpx.Response(200, json={"status": "ok", "message": {"items": "nope"}}),
        httpx.Response(200, json={"unexpected": True}),
    ],
)
async def test_unexpected_payload_is_a_provider_error(provider, response):
    respx.get(WORKS_URL).mock(return_value=response)

    with pytest.raises(ProviderError, match="unexpected response"):
        await provider.search("x")


# --- settings ---------------------------------------------------------------------------------


def test_rate_limit_depends_on_polite_pool():
    base = {"_env_file": None, "ncbi_email": "a@b.co"}
    assert Settings(**base).crossref_requests_per_second == 1
    assert Settings(**base, crossref_mailto="ops@example.org").crossref_requests_per_second == 3


def test_blank_mailto_means_not_configured():
    assert Settings(_env_file=None, ncbi_email="a@b.co", crossref_mailto="  ").crossref_mailto is None


def test_mailto_must_look_like_an_email():
    with pytest.raises(ValueError):
        Settings(_env_file=None, ncbi_email="a@b.co", crossref_mailto="not-an-email")


@pytest.mark.parametrize("rows", [0, 101])
def test_rows_out_of_bounds_is_rejected(rows):
    with pytest.raises(ValueError):
        Settings(_env_file=None, ncbi_email="a@b.co", crossref_rows=rows)


def test_mailto_is_read_from_environment(monkeypatch):
    monkeypatch.setenv("NCBI_EMAIL", "a@b.co")
    monkeypatch.setenv("CROSSREF_MAILTO", "ops@example.org")
    assert Settings(_env_file=None).crossref_mailto == "ops@example.org"
