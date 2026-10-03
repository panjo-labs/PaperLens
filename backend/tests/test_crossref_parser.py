import json

import pytest

from app.providers.crossref_parser import CrossrefParseError, parse_crossref_response
from tests.conftest import FIXTURES


@pytest.fixture
def sample() -> dict:
    return json.loads((FIXTURES / "crossref_sample.json").read_text(encoding="utf-8"))


def envelope(*items) -> dict:
    return {"status": "ok", "message-type": "work-list", "message": {"items": list(items)}}


def work(**overrides) -> dict:
    record = {"DOI": "10.1/abc", "title": ["A title"]}
    record.update(overrides)
    return record


def one(**overrides):
    (paper,) = parse_crossref_response(envelope(work(**overrides)))
    return paper


# --- recorded real-world fixture --------------------------------------------------------------


def test_fixture_parses_all_records(sample):
    papers = parse_crossref_response(sample)
    assert [p.source_id for p in papers] == [item["DOI"] for item in sample["message"]["items"]]
    assert all(p.source == "crossref" and p.id == f"crossref:{p.source_id}" for p in papers)
    assert all(p.doi == p.source_id for p in papers)


def by_doi(sample):
    return {p.doi: p for p in parse_crossref_response(sample)}


def test_complete_record_is_normalized(sample):
    paper = by_doi(sample)["10.5348/100041d05pa2018ra"]
    assert paper.title.startswith("Comparative effectiveness of cognitive behavioural therapy")
    assert paper.authors[:2] == ["Paapa Kwesi Ampiah", "Paul Hendrick"]
    assert paper.journal == "Edorium Journal of Disability and Rehabilitation"
    assert paper.publication_date == "2018-05-28"
    assert paper.url == "https://doi.org/10.5348/100041d05pa2018ra"


def test_jats_abstract_becomes_plain_text(sample):
    paper = by_doi(sample)["10.5348/100041d05pa2018ra"]
    assert paper.abstract.startswith("Aims: To systematically review")
    assert "<" not in paper.abstract and "\t" not in paper.abstract


def test_multi_paragraph_abstract_keeps_paragraphs_on_separate_lines(sample):
    paper = by_doi(sample)["10.36283/pjr.zu.14.2/004"]
    assert paper.abstract.startswith("Background: ")
    assert "\n" in paper.abstract


def test_missing_authors_and_abstract_are_empty_and_none(sample):
    paper = by_doi(sample)["10.29011/2576-957x.100028"]
    assert paper.authors == []
    assert paper.abstract is None


def test_html_entities_in_journal_title_are_decoded(sample):
    assert by_doi(sample)["10.29011/2576-957x.100028"].journal == "Chronic Pain & Management"


def test_partial_dates_keep_their_precision(sample):
    papers = by_doi(sample)
    assert papers["10.29011/2576-957x.100028"].publication_date == "2020"
    assert papers["10.1016/j.explore.2022.08.012"].publication_date == "2022-11"
    assert papers["10.1922/pjs.16.1s.2026.518"].publication_date == "2026-06-12"


def test_family_name_only_author_is_kept(sample):
    paper = by_doi(sample)["10.1922/pjs.16.1s.2026.518"]
    assert paper.authors[0] == "Dr Ananya Sharma"


# --- missing / odd metadata -------------------------------------------------------------------


def test_record_without_doi_is_skipped():
    assert parse_crossref_response(envelope({"title": ["No DOI"]})) == []


def test_record_without_title_is_skipped():
    assert parse_crossref_response(envelope({"DOI": "10.1/x"}, {"DOI": "10.1/y", "title": []})) == []


def test_only_provided_fields_are_populated():
    paper = one()
    assert (paper.authors, paper.abstract, paper.journal, paper.publication_date, paper.url) == (
        [],
        None,
        None,
        None,
        None,
    )
    assert paper.doi == "10.1/abc"  # the provider's own record key, not a guess


def test_url_is_not_constructed_from_the_doi():
    assert one().url is None


@pytest.mark.parametrize("url", ["javascript:alert(1)", "ftp://x/y", "", 5, None, ["https://a"]])
def test_non_web_urls_are_dropped(url):
    assert one(URL=url).url is None


def test_web_url_is_kept():
    assert one(URL="https://doi.org/10.1/abc").url == "https://doi.org/10.1/abc"


@pytest.mark.parametrize(
    ("issued", "expected"),
    [
        ({"date-parts": [[2024]]}, "2024"),
        ({"date-parts": [[2024, 3]]}, "2024-03"),
        ({"date-parts": [[2024, 3, 5]]}, "2024-03-05"),
        ({"date-parts": [[2024, None]]}, "2024"),
        ({"date-parts": [[2024, 13]]}, "2024"),  # impossible month: keep only what is valid
        ({"date-parts": [[2024, 3, 40]]}, "2024-03"),
        ({"date-parts": [[None]]}, None),  # Crossref's way of saying "unknown"
        ({"date-parts": [[]]}, None),
        ({"date-parts": []}, None),
        ({"date-parts": [["2024"]]}, None),  # strings are not trusted
        ({"date-parts": [[True]]}, None),
        ({"date-parts": "2024"}, None),
        ("2024", None),
        ({}, None),
    ],
)
def test_publication_date_variants(issued, expected):
    assert one(issued=issued).publication_date == expected


def test_publication_date_falls_back_to_other_date_fields():
    paper = one(issued={"date-parts": [[None]]}, **{"published-online": {"date-parts": [[2021, 7]]}})
    assert paper.publication_date == "2021-07"


def test_creation_date_is_never_used_as_publication_date():
    assert one(created={"date-parts": [[2030, 1, 1]]}).publication_date is None


def test_author_variants():
    authors = [
        {"given": "Jane", "family": "Smith"},
        {"family": "Lee"},
        {"given": "Solo"},
        {"name": "Some Consortium"},
        {"given": "  ", "family": ""},
        {},
        "not a dict",
        None,
    ]
    assert one(author=authors).authors == ["Jane Smith", "Lee", "Solo", "Some Consortium"]


def test_markup_and_entities_in_title_are_cleaned():
    paper = one(title=["Effect of <i>in vivo</i> CO<sub>2</sub> &amp; pain"])
    assert paper.title == "Effect of in vivo CO2 & pain"


def test_decoded_angle_brackets_stay_text():
    assert one(title=["p &lt; 0.05 in &lt;b&gt;trials"]).title == "p < 0.05 in <b>trials"


def test_title_may_be_a_bare_string_and_first_non_empty_wins():
    assert one(title="Bare").title == "Bare"
    assert one(title=["", "  ", "Second"]).title == "Second"


def test_journal_is_first_container_title():
    assert one(**{"container-title": ["J One", "J Two"]}).journal == "J One"
    assert one(**{"container-title": []}).journal is None


def test_doi_prefix_is_stripped():
    assert one(DOI="https://doi.org/10.1/abc").doi == "10.1/abc"


@pytest.mark.parametrize(
    ("abstract", "expected"),
    [
        ("<jats:p>Plain paragraph.</jats:p>", "Plain paragraph."),
        ("No markup at all", "No markup at all"),
        ("<jats:title>Abstract</jats:title><jats:p>Body.</jats:p>", "Body."),
        (
            "<jats:sec><jats:title>Background</jats:title><jats:p>A.</jats:p></jats:sec>"
            "<jats:sec><jats:title>Methods</jats:title><jats:p>B.</jats:p></jats:sec>",
            "Background: A.\nMethods: B.",
        ),
        ("<jats:p>Rate was <jats:italic>p</jats:italic> &lt; 0.05 &amp; stable</jats:p>", "Rate was p < 0.05 & stable"),
        ("<jats:p>  </jats:p>", None),
        ("", None),
        ("Abstract", None),
        (None, None),
        (["not", "a", "string"], None),
        (42, None),
    ],
)
def test_abstract_variants(abstract, expected):
    assert one(abstract=abstract).abstract == expected


# --- malformed records ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [None, 5, "text", ["list"], {"DOI": 5, "title": ["T"]}, {"DOI": ["x"], "title": ["T"]}, {"DOI": "10.1/x", "title": {"a": 1}}],
)
def test_malformed_records_are_skipped_without_affecting_others(bad):
    papers = parse_crossref_response(envelope(bad, work(DOI="10.1/good")))
    assert [p.doi for p in papers] == ["10.1/good"]


def test_wrongly_typed_optional_fields_are_ignored_not_fatal():
    paper = one(author="oops", abstract={"x": 1}, **{"container-title": 5, "issued": [1, 2]})
    assert (paper.authors, paper.abstract, paper.journal, paper.publication_date) == ([], None, None, None)


def test_empty_item_list():
    assert parse_crossref_response(envelope()) == []


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "ok",
        {},
        {"status": "failed", "message": {"items": []}},
        {"status": "ok"},
        {"status": "ok", "message": None},
        {"status": "ok", "message": {"items": "nope"}},
        {"status": "ok", "message": {}},
    ],
)
def test_wrong_top_level_shape_raises(payload):
    with pytest.raises(CrossrefParseError):
        parse_crossref_response(payload)


# --- hostile / garbled markup must not stall the event loop --------------------------------------


@pytest.mark.parametrize(
    ("field", "payload"),
    [
        # explicit ids: pytest puts the test id in an env var, which Windows caps at 32K chars
        pytest.param("abstract", "<jats:title>" * 8_000 + "x" * 8_000, id="unclosed-jats-titles"),
        pytest.param("abstract", "<" * 50_000 + "a" * 50_000, id="unclosed-angle-brackets"),
        pytest.param("abstract", "<jats:title" * 90_000, id="tag-never-closed"),
        pytest.param("title", ["<a " * 100_000], id="garbled-title"),
        pytest.param("container-title", ["<" * 100_000], id="garbled-journal"),
    ],
)
def test_garbled_markup_is_processed_in_linear_time(field, payload):
    import time

    start = time.perf_counter()
    parse_crossref_response(envelope(work(**{field: payload})))
    assert time.perf_counter() - start < 2  # was effectively unbounded before the single-pass rewrite
