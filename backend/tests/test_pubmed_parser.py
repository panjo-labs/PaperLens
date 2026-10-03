import pytest

from app.providers.pubmed_parser import PubMedParseError, parse_pubmed_xml


def by_pmid(papers):
    return {p.source_id: p for p in papers}


def article(
    pmid="1",
    title="A title",
    pub_date="<Year>2020</Year>",
    abstract="",
    authors="",
    article_ids="",
    elocation="",
    extra="",
):
    return f"""<PubmedArticle>
      <MedlineCitation><PMID Version="1">{pmid}</PMID>
        <Article>
          <Journal><Title>J Test</Title><JournalIssue><PubDate>{pub_date}</PubDate></JournalIssue></Journal>
          <ArticleTitle>{title}</ArticleTitle>
          {elocation}{abstract}
          <AuthorList>{authors}</AuthorList>
        </Article>
      </MedlineCitation>
      <PubmedData><ArticleIdList>{article_ids}</ArticleIdList>{extra}</PubmedData>
    </PubmedArticle>"""


def wrap(*articles):
    return f"<PubmedArticleSet>{''.join(articles)}</PubmedArticleSet>"


def one(xml_article):
    (paper,) = parse_pubmed_xml(wrap(xml_article))
    return paper


# --- recorded real-world fixtures -------------------------------------------------------------


def test_fixture_parses_all_records(efetch_xml):
    papers = parse_pubmed_xml(efetch_xml)
    assert {p.source_id for p in papers} == {
        "42806217",
        "42802597",
        "42813137",
        "39306741",
        "42602955",
    }


def test_common_fields_are_populated(efetch_xml):
    paper = by_pmid(parse_pubmed_xml(efetch_xml))["42806217"]
    assert paper.id == "pubmed:42806217"
    assert paper.source == "pubmed"
    assert paper.url == "https://pubmed.ncbi.nlm.nih.gov/42806217/"
    assert paper.journal == "European journal of pain (London, England)"
    assert paper.title.startswith("Effectiveness, Mediators and Moderators")
    assert paper.authors[0] == "Carlos Gevers-Montoro"


def test_structured_abstract_keeps_section_labels(efetch_xml):
    paper = by_pmid(parse_pubmed_xml(efetch_xml))["42806217"]
    lines = paper.abstract.split("\n")
    assert len(lines) == 5
    assert lines[0].startswith("BACKGROUND: ")
    assert lines[1].startswith("METHODS: ")


def test_unstructured_abstract_has_no_label(efetch_xml):
    paper = by_pmid(parse_pubmed_xml(efetch_xml))["42802597"]
    assert paper.abstract.startswith("Chronic low back pain")
    assert "\n" not in paper.abstract


def test_missing_abstract_is_none(efetch_xml):
    paper = by_pmid(parse_pubmed_xml(efetch_xml))["39306741"]
    assert paper.abstract is None


def test_missing_doi_is_none(efetch_xml):
    paper = by_pmid(parse_pubmed_xml(efetch_xml))["42813137"]
    assert paper.doi is None


def test_doi_comes_from_the_article_not_its_reference_list(efetch_xml):
    # This record's reference list also contains DOIs (e.g. an SSRN preprint).
    paper = by_pmid(parse_pubmed_xml(efetch_xml))["42806217"]
    assert paper.doi == "10.1002/ejp.70388"


def test_collective_author_is_kept_alongside_personal_authors(efetch_xml):
    paper = by_pmid(parse_pubmed_xml(efetch_xml))["42602955"]
    assert "Back2Health Consortium" in paper.authors
    assert "Willeke Boonstra" in paper.authors
    assert len(paper.authors) == 9


# --- targeted cases ---------------------------------------------------------------------------


def test_doi_from_reference_list_is_not_used_when_article_has_none():
    xml = article(
        article_ids='<ArticleId IdType="pubmed">1</ArticleId>',
        extra=(
            '<ReferenceList><Reference><ArticleIdList>'
            '<ArticleId IdType="doi">10.9999/someone-elses</ArticleId>'
            "</ArticleIdList></Reference></ReferenceList>"
        ),
    )
    assert one(xml).doi is None


def test_doi_falls_back_to_elocation_id():
    xml = article(elocation='<ELocationID EIdType="doi" ValidYN="Y">10.1/abc</ELocationID>')
    assert one(xml).doi == "10.1/abc"


def test_doi_url_prefix_is_stripped():
    xml = article(article_ids='<ArticleId IdType="doi">https://doi.org/10.1/abc</ArticleId>')
    assert one(xml).doi == "10.1/abc"


@pytest.mark.parametrize(
    ("pub_date", "expected"),
    [
        ("<Year>2024</Year>", "2024"),
        ("<Year>2024</Year><Month>Mar</Month>", "2024-03"),
        ("<Year>2024</Year><Month>03</Month>", "2024-03"),
        ("<Year>2024</Year><Month>Mar</Month><Day>5</Day>", "2024-03-05"),
        ("<Year>2024</Year><Month>Spring</Month>", "2024"),
        ("<Year>2024</Year><Day>5</Day>", "2024"),  # day without month is not usable precision
        ("<MedlineDate>2024 Jan-Feb</MedlineDate>", "2024"),
        ("<MedlineDate>Winter</MedlineDate>", None),
    ],
)
def test_publication_date_preserves_provided_precision(pub_date, expected):
    assert one(article(pub_date=pub_date)).publication_date == expected


def test_publication_date_falls_back_to_article_date():
    xml = article(pub_date="<MedlineDate>Winter</MedlineDate>").replace(
        "<AuthorList>",
        "<ArticleDate><Year>2023</Year><Month>11</Month><Day>02</Day></ArticleDate><AuthorList>",
    )
    assert one(xml).publication_date == "2023-11-02"


def test_inline_markup_in_title_and_abstract_is_flattened():
    xml = article(
        title="Effect of <i>in vivo</i> CO<sub>2</sub> on pain",
        abstract='<Abstract><AbstractText>Level of <b>H</b><sup>+</sup>\n   rose.</AbstractText></Abstract>',
    )
    paper = one(xml)
    assert paper.title == "Effect of in vivo CO2 on pain"
    assert paper.abstract == "Level of H+ rose."


def test_empty_abstract_text_is_none():
    assert one(article(abstract="<Abstract><AbstractText>  </AbstractText></Abstract>")).abstract is None


def test_author_name_variants():
    authors = (
        "<Author><LastName>Smith</LastName><ForeName>Jane A</ForeName><Initials>JA</Initials></Author>"
        "<Author><LastName>Lee</LastName><Initials>K</Initials></Author>"
        "<Author><CollectiveName>Some Study Group</CollectiveName></Author>"
        "<Author><Initials>X</Initials></Author>"
    )
    assert one(article(authors=authors)).authors == ["Jane A Smith", "K Lee", "Some Study Group", ]


def test_records_without_pmid_or_title_are_skipped():
    good = article(pmid="7")
    no_title = article(pmid="8", title="")
    no_pmid = article(pmid="")
    papers = parse_pubmed_xml(wrap(no_title, good, no_pmid))
    assert [p.source_id for p in papers] == ["7"]


def test_empty_result_set():
    assert parse_pubmed_xml("<PubmedArticleSet/>") == []


def test_non_article_records_are_ignored():
    xml = wrap("<PubmedBookArticle><BookDocument/></PubmedBookArticle>", article(pmid="9"))
    assert [p.source_id for p in parse_pubmed_xml(xml)] == ["9"]


@pytest.mark.parametrize("payload", [b"", b"not xml at all", b"<PubmedArticleSet><oops>"])
def test_malformed_xml_raises(payload):
    with pytest.raises(PubMedParseError):
        parse_pubmed_xml(payload)


def test_entity_expansion_is_rejected():
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
        "<PubmedArticleSet>&b;</PubmedArticleSet>"
    )
    with pytest.raises(PubMedParseError):
        parse_pubmed_xml(bomb)
