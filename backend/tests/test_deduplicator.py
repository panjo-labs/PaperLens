import pytest

from app.schemas.paper import Paper
from app.services.deduplicator import deduplicate, normalize_doi, normalize_title


def pubmed(pmid="1", title="A title", doi=None, **extra) -> Paper:
    return Paper(
        id=f"pubmed:{pmid}", title=title, authors=extra.pop("authors", []), doi=doi,
        source="pubmed", source_id=pmid, **extra,
    )


def crossref(doi="10.1/x", title="A title", **extra) -> Paper:
    return Paper(
        id=f"crossref:{doi}", title=title, authors=extra.pop("authors", []), doi=doi,
        source="crossref", source_id=doi, **extra,
    )


def other(source_id="a", title="A title", **extra) -> Paper:
    """A record with NO doi and NO pubmed id: the only kind the title rule may merge."""
    return Paper(
        id=f"other:{source_id}", title=title, authors=[], source="other", source_id=source_id, **extra
    )


# --- normalization helpers --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10.1000/ABC", "10.1000/abc"),
        ("  10.1000/abc  ", "10.1000/abc"),
        ("https://doi.org/10.1000/abc", "10.1000/abc"),
        ("http://doi.org/10.1000/abc", "10.1000/abc"),
        ("HTTPS://DOI.ORG/10.1000/ABC", "10.1000/abc"),
        ("https://dx.doi.org/10.1000/abc", "10.1000/abc"),
        ("doi:10.1000/abc", "10.1000/abc"),
        ("DOI: 10.1000/abc", "10.1000/abc"),
        ("", None),
        ("   ", None),
        ("doi:", None),
        (None, None),
    ],
)
def test_normalize_doi(raw, expected):
    assert normalize_doi(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Low-Back Pain: A Review!", "low back pain a review"),
        ("  Exercise   therapy\n for pain ", "exercise therapy for pain"),
        ("Guillain-Barré syndrome", "guillain barre syndrome"),
        ("Effect of CO₂ on pain", "effect of co2 on pain"),  # NFKD turns the subscript 2 into 2
        ("“Smart” quotes – dashes", "smart quotes dashes"),
        ("???", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_title(raw, expected):
    assert normalize_title(raw) == expected


# --- merging duplicates -----------------------------------------------------------------------


def test_same_doi_from_pubmed_and_crossref_is_one_paper():
    result = deduplicate([pubmed("1", doi="10.1000/abc"), crossref("10.1000/abc")])

    assert len(result) == 1
    assert result[0].id == "pubmed:1"  # the first record leads


@pytest.mark.parametrize(
    "crossref_doi",
    ["10.1000/ABC", "https://doi.org/10.1000/abc", "http://doi.org/10.1000/ABC", "doi:10.1000/abc", " 10.1000/abc "],
)
def test_doi_in_different_formats_is_still_the_same_paper(crossref_doi):
    result = deduplicate([pubmed("1", doi="10.1000/abc"), crossref(crossref_doi)])
    assert len(result) == 1


def test_same_pubmed_id_is_the_same_paper():
    result = deduplicate([pubmed("42", title="First copy"), pubmed("42", title="Second copy")])

    assert [p.id for p in result] == ["pubmed:42"]
    assert result[0].title == "First copy"


def test_same_normalized_title_without_doi_or_pmid_is_the_same_paper():
    result = deduplicate(
        [other("a", "Low-Back Pain: a REVIEW"), other("b", "low back pain  a review!")]
    )

    assert len(result) == 1
    assert result[0].source_ids == ["other:a", "other:b"]


def test_similar_but_not_identical_titles_stay_separate():
    result = deduplicate(
        [
            other("a", "Exercise therapy for low back pain"),
            other("b", "Exercise therapy for chronic low back pain"),
            other("c", "Exercise therapy for low back pain: a protocol"),
        ]
    )
    assert len(result) == 3


def test_same_title_but_different_dois_are_different_papers():
    # e.g. successive versions of a Cochrane review share a title but have their own DOIs.
    result = deduplicate(
        [
            crossref("10.1002/14651858.cd000335", "Exercise therapy for low back pain"),
            crossref("10.1002/14651858.cd000335.pub2", "Exercise therapy for low back pain"),
        ]
    )
    assert len(result) == 2


def test_same_title_but_different_pubmed_ids_are_different_papers():
    assert len(deduplicate([pubmed("1", "Editorial"), pubmed("2", "Editorial")])) == 2


def test_title_alone_never_merges_records_that_already_have_an_identity():
    # A PubMed record (has a PMID) and a Crossref record (has a DOI) with the same title are
    # NOT merged: they might be different papers, and each already has a trustworthy identity.
    result = deduplicate([pubmed("1", "Same title"), crossref("10.1/x", "Same title")])
    assert len(result) == 2


def test_a_record_without_identity_is_not_merged_into_one_that_has_it_by_title():
    assert len(deduplicate([pubmed("1", "Same title"), other("a", "Same title")])) == 2


def test_duplicates_are_found_through_a_chain_of_shared_keys():
    a = crossref("10.1/x", "T", authors=["A"])
    b = pubmed("7", "T", doi="10.1/x")  # shares the DOI with a
    c = pubmed("7", "T")  # shares the PMID with b
    result = deduplicate([a, b, c])

    assert len(result) == 1
    assert result[0].source_ids == ["crossref:10.1/x", "pubmed:7"]


def test_unrelated_papers_are_all_kept_in_first_seen_order():
    papers = [pubmed("3", "C", doi="10.1/c"), crossref("10.1/a", "A"), pubmed("1", "B")]
    assert [p.id for p in deduplicate(papers)] == ["pubmed:3", "crossref:10.1/a", "pubmed:1"]


def test_empty_blank_or_symbol_only_values_never_cause_a_merge():
    papers = [
        crossref("10.1/a", "???"),
        crossref("10.1/b", "???"),
        other("a", "!!!"),
        other("b", "!!!"),
        pubmed("1", "T1", doi=" "),
        pubmed("2", "T2", doi=""),
    ]
    assert len(deduplicate(papers)) == 6


def test_empty_input_and_single_paper():
    assert deduplicate([]) == []
    only = pubmed("1")
    assert deduplicate([only]) == [only]


# --- provenance ------------------------------------------------------------------------------


def test_provenance_is_preserved_when_merging():
    merged = deduplicate([pubmed("123", doi="10.1000/abc"), crossref("10.1000/abc")])[0]

    assert merged.id == "pubmed:123"
    assert merged.source == "pubmed" and merged.source_id == "123"
    assert merged.doi == "10.1000/abc"
    assert merged.source_ids == ["pubmed:123", "crossref:10.1000/abc"]


def test_unmerged_paper_lists_only_itself():
    assert pubmed("5").source_ids == ["pubmed:5"]
    assert deduplicate([pubmed("5")])[0].source_ids == ["pubmed:5"]


def test_three_way_merge_keeps_every_source_once():
    papers = [pubmed("1", doi="10.1/x"), crossref("10.1/x"), crossref("HTTPS://DOI.ORG/10.1/X")]
    merged = deduplicate(papers)[0]
    assert merged.source_ids == ["pubmed:1", "crossref:10.1/x", "crossref:HTTPS://DOI.ORG/10.1/X"]


def test_pmid_stays_available_after_the_crossref_record_leads():
    # If a caller lists Crossref first, the PMID must not be lost from the merged record.
    merged = deduplicate([crossref("10.1/x"), pubmed("99", doi="10.1/x")])[0]
    assert merged.id == "crossref:10.1/x"
    assert "pubmed:99" in merged.source_ids


# --- metadata merging ------------------------------------------------------------------------


def test_abstract_from_the_record_that_has_one_is_kept():
    merged = deduplicate(
        [pubmed("1", doi="10.1/x", abstract="Full abstract text."), crossref("10.1/x")]
    )[0]
    assert merged.abstract == "Full abstract text."

    merged = deduplicate(
        [pubmed("1", doi="10.1/x"), crossref("10.1/x", abstract="Crossref abstract.")]
    )[0]
    assert merged.abstract == "Crossref abstract."


def test_longest_abstract_wins_when_both_have_one():
    merged = deduplicate(
        [pubmed("1", doi="10.1/x", abstract="Short."), crossref("10.1/x", abstract="A much longer abstract.")]
    )[0]
    assert merged.abstract == "A much longer abstract."


def test_missing_doi_is_filled_from_another_record_of_the_same_paper():
    no_doi = pubmed("1")
    with_doi = Paper(
        id="crossref:10.1/x", title="A title", authors=[], doi="10.1/x", source="crossref",
        source_id="10.1/x", source_ids=["crossref:10.1/x", "pubmed:1"],  # known to be PMID 1 as well
    )
    merged = deduplicate([no_doi, with_doi])[0]

    assert merged.doi == "10.1/x"
    assert merged.id == "pubmed:1"


def test_more_authors_more_precise_date_first_journal_and_url_are_used():
    a = pubmed("1", doi="10.1/x", authors=["A"], publication_date="2024", journal=None, url=None)
    b = crossref(
        "10.1/x", authors=["A", "B"], publication_date="2024-03-15", journal="Spine",
        url="https://doi.org/10.1/x",
    )
    c = crossref("HTTPS://doi.org/10.1/X", journal="Other Journal", authors=["Z"], publication_date="2024-03")
    merged = deduplicate([a, b, c])[0]

    assert merged.authors == ["A", "B"]
    assert merged.publication_date == "2024-03-15"
    assert merged.journal == "Spine"
    assert merged.url == "https://doi.org/10.1/x"


def test_base_record_wins_for_title_and_value_present_in_base_is_not_replaced():
    merged = deduplicate(
        [pubmed("1", "PubMed title", doi="10.1/x", journal="PubMed Journal"), crossref("10.1/x", "Crossref title", journal="X")]
    )[0]
    assert merged.title == "PubMed title"
    assert merged.journal == "PubMed Journal"


def test_nothing_is_invented_when_every_record_lacks_a_field():
    merged = deduplicate([pubmed("1", doi="10.1/x"), crossref("10.1/x")])[0]

    assert merged.abstract is None
    assert merged.journal is None
    assert merged.publication_date is None
    assert merged.authors == []
    assert merged.url is None


def test_merging_resets_a_stale_rank_score():
    merged = deduplicate(
        [pubmed("1", doi="10.1/x", rank_score=0.9), crossref("10.1/x", rank_score=0.1)]
    )[0]
    assert merged.rank_score is None


def test_inputs_are_not_mutated():
    a, b = pubmed("1", doi="10.1/x", authors=["A"]), crossref("10.1/x", authors=["A", "B"])
    deduplicate([a, b])

    assert a.authors == ["A"] and a.source_ids == ["pubmed:1"]
    assert b.source_ids == ["crossref:10.1/x"]
