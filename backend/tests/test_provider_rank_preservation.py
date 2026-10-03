"""Each provider's original result position must survive:

    provider -> combined results -> deduplication -> ranking

`Paper.provider_ranks` ({"pubmed": 3, "crossref": 12}) is not used for ordering yet; it is kept so a
future ranker can fuse it with our own score. These tests make sure it is never lost or altered.
"""

import pytest

from app.schemas.paper import Paper
from app.services.deduplicator import deduplicate
from app.services.orchestrator import ResearchOrchestrator
from app.services.ranker import rank_papers


def paper(source, source_id, title="Unrelated", doi=None, ranks=None, **extra) -> Paper:
    return Paper(
        id=f"{source}:{source_id}", title=title, authors=[], doi=doi, source=source,
        source_id=source_id, provider_ranks=ranks or {}, **extra,
    )


QUESTION = "yoga for chronic back pain"


# --- the model --------------------------------------------------------------------------------


def test_provider_ranks_defaults_to_empty_and_does_not_break_existing_construction():
    p = Paper(id="x:1", title="T", authors=[], source="x", source_id="1")
    assert p.provider_ranks == {}
    assert p.model_dump()["provider_ranks"] == {}


def test_each_paper_gets_its_own_dict():
    a = Paper(id="x:1", title="T", authors=[], source="x", source_id="1")
    b = Paper(id="x:2", title="T", authors=[], source="x", source_id="2")
    a.provider_ranks["x"] = 1
    assert b.provider_ranks == {}


# --- stage 1: provider -> combined results (the orchestrator stamps the positions) -----------------


async def test_orchestrator_records_each_papers_original_position(fake_provider):
    provider = fake_provider("pubmed", [
        paper("pubmed", "1", "Unrelated cats"),
        paper("pubmed", "2", "Yoga for chronic back pain"),  # 2nd for the provider, best match
        paper("pubmed", "3", "Gardening"),
    ])

    result = await ResearchOrchestrator([provider]).research(QUESTION)

    ranks = {p.source_id: p.provider_ranks for p in result.papers}
    assert ranks == {"1": {"pubmed": 1}, "2": {"pubmed": 2}, "3": {"pubmed": 3}}
    assert result.papers[0].source_id == "2"  # ranking moved it to the front ...
    assert result.papers[0].provider_ranks == {"pubmed": 2}  # ... but its original position is intact


async def test_positions_count_within_each_provider_not_across_the_combined_list(fake_provider):
    pm = fake_provider("pubmed", [paper("pubmed", "1"), paper("pubmed", "2")])
    # Real Crossref records always have a DOI (without one, identical titles would merge by the title fallback).
    cr = fake_provider("crossref", [paper("crossref", "10.1/a", doi="10.1/a"), paper("crossref", "10.1/b", doi="10.1/b")])

    result = await ResearchOrchestrator([pm, cr]).research(QUESTION)

    ranks = {p.id: p.provider_ranks for p in result.papers}
    assert ranks["crossref:10.1/a"] == {"crossref": 1}  # first for Crossref, though 3rd in the combined list
    assert ranks["crossref:10.1/b"] == {"crossref": 2}
    assert ranks["pubmed:1"] == {"pubmed": 1}


async def test_positions_follow_provider_order_not_completion_order(fake_provider):
    slow = fake_provider("pubmed", [paper("pubmed", "1"), paper("pubmed", "2")], delay=0.1)
    fast = fake_provider("crossref", [paper("crossref", "10.1/a", doi="10.1/a")], delay=0.0)

    result = await ResearchOrchestrator([slow, fast]).research(QUESTION)

    assert {p.id: p.provider_ranks for p in result.papers} == {
        "pubmed:1": {"pubmed": 1}, "pubmed:2": {"pubmed": 2}, "crossref:10.1/a": {"crossref": 1},
    }


# --- stage 2: deduplication ---------------------------------------------------------------------


async def test_both_positions_survive_a_cross_provider_merge(fake_provider):
    pm = fake_provider("pubmed", [paper("pubmed", "1", "Other"), paper("pubmed", "2", "Same paper", doi="10.1/same")])
    cr = fake_provider("crossref", [paper("crossref", "10.1/x", doi="10.1/x"), paper("crossref", "10.1/SAME", "Same paper", doi="10.1/SAME")])

    result = await ResearchOrchestrator([pm, cr]).research(QUESTION)

    merged = next(p for p in result.papers if p.id == "pubmed:2")
    assert merged.source_ids == ["pubmed:2", "crossref:10.1/SAME"]
    assert merged.provider_ranks == {"pubmed": 2, "crossref": 2}  # 2nd in PubMed's list AND 2nd in Crossref's
    assert len(result.papers) == 3


def test_dedup_unions_ranks_of_different_providers():
    merged = deduplicate([
        paper("pubmed", "1", doi="10.1/x", ranks={"pubmed": 3}),
        paper("crossref", "10.1/x", doi="10.1/x", ranks={"crossref": 12}),
    ])[0]
    assert merged.provider_ranks == {"pubmed": 3, "crossref": 12}


def test_dedup_keeps_the_better_position_when_a_provider_listed_the_paper_twice():
    merged = deduplicate([
        paper("crossref", "10.1/x", doi="10.1/x", ranks={"crossref": 9}),
        paper("crossref", "10.1/X", doi="10.1/X", ranks={"crossref": 4}),
        paper("crossref", "https://doi.org/10.1/x", doi="https://doi.org/10.1/x", ranks={"crossref": 17}),
    ])[0]
    assert merged.provider_ranks == {"crossref": 4}


def test_dedup_leaves_unmerged_papers_ranks_untouched():
    result = deduplicate([
        paper("pubmed", "1", ranks={"pubmed": 1}),
        paper("pubmed", "2", ranks={"pubmed": 2}),
        paper("crossref", "10.1/a", doi="10.1/a", ranks={"crossref": 1}),
    ])
    assert [p.provider_ranks for p in result] == [{"pubmed": 1}, {"pubmed": 2}, {"crossref": 1}]


def test_dedup_handles_papers_that_have_no_ranks():
    merged = deduplicate([paper("pubmed", "1", doi="10.1/x"), paper("crossref", "10.1/x", doi="10.1/x")])[0]
    assert merged.provider_ranks == {}


def test_dedup_does_not_mutate_the_input_ranks():
    a = paper("pubmed", "1", doi="10.1/x", ranks={"pubmed": 3})
    b = paper("crossref", "10.1/x", doi="10.1/x", ranks={"crossref": 12})
    deduplicate([a, b])
    assert a.provider_ranks == {"pubmed": 3} and b.provider_ranks == {"crossref": 12}


# --- stage 3: ranking ---------------------------------------------------------------------------


def test_ranking_reorders_papers_but_never_changes_their_provider_ranks():
    papers = [
        paper("pubmed", "1", "Unrelated", ranks={"pubmed": 1}),
        paper("pubmed", "2", "Another unrelated", ranks={"pubmed": 2}),
        paper("pubmed", "3", "Yoga for chronic back pain", ranks={"pubmed": 3}),  # best match, 3rd for the provider
    ]
    ranked = rank_papers(QUESTION, papers)

    assert [p.source_id for p in ranked] == ["3", "1", "2"]
    assert {p.source_id: p.provider_ranks for p in ranked} == {
        "1": {"pubmed": 1}, "2": {"pubmed": 2}, "3": {"pubmed": 3},
    }


@pytest.mark.parametrize("ranks_a, ranks_b", [({"pubmed": 1}, {"pubmed": 50}), ({"pubmed": 50}, {"pubmed": 1})])
def test_ranking_does_not_use_provider_ranks_yet(ranks_a, ranks_b):
    """Not redesigning the ranker: two papers identical except for provider position order the same either way."""
    papers = [paper("pubmed", "a", "Yoga back pain", ranks=ranks_a), paper("pubmed", "b", "Yoga back pain", ranks=ranks_b)]
    assert [p.source_id for p in rank_papers(QUESTION, papers)] == ["a", "b"]  # decided by the id tie-breaker only


def test_ranking_does_not_change_the_input_ranks():
    original = paper("pubmed", "1", "Yoga", ranks={"pubmed": 7})
    rank_papers(QUESTION, [original])
    assert original.provider_ranks == {"pubmed": 7}


# --- all three stages together --------------------------------------------------------------------


async def test_provider_to_combined_to_dedup_to_ranking_end_to_end(fake_provider):
    pm = fake_provider("pubmed", [
        paper("pubmed", "1", "Gardening tips"),
        paper("pubmed", "2", "Yoga for chronic back pain", doi="10.1/yoga"),
        paper("pubmed", "3", "Back pain and yoga", doi="10.1/b"),
    ])
    cr = fake_provider("crossref", [
        paper("crossref", "10.1/q", "Quilting", doi="10.1/q"),
        paper("crossref", "10.1/yoga", "Yoga for chronic back pain", doi="10.1/yoga"),
    ])

    result = await ResearchOrchestrator([pm, cr]).research(QUESTION)

    by_id = {p.id: p for p in result.papers}
    assert len(result.papers) == 4  # 5 raw - 1 duplicate
    # the merged paper remembers it was PubMed's 2nd and Crossref's 2nd choice
    assert by_id["pubmed:2"].provider_ranks == {"pubmed": 2, "crossref": 2}
    assert by_id["pubmed:1"].provider_ranks == {"pubmed": 1}
    assert by_id["pubmed:3"].provider_ranks == {"pubmed": 3}
    assert by_id["crossref:10.1/q"].provider_ranks == {"crossref": 1}
    # ranking happened (best match first) without disturbing any of the above
    assert result.papers[0].id == "pubmed:2"
    assert all(p.rank_score is not None for p in result.papers)


async def test_a_failed_provider_contributes_no_positions_and_the_others_are_unaffected(fake_provider):
    from app.providers.base import ProviderError

    ok = fake_provider("pubmed", [paper("pubmed", "1"), paper("pubmed", "2")])
    bad = fake_provider("crossref", error=ProviderError("crossref", "HTTP 503 after retry"))

    result = await ResearchOrchestrator([ok, bad]).research(QUESTION)

    assert {p.id: p.provider_ranks for p in result.papers} == {"pubmed:1": {"pubmed": 1}, "pubmed:2": {"pubmed": 2}}
