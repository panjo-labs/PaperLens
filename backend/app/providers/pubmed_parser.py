"""Parse PubMed efetch XML into the common Paper model.

PubMed returns XML shaped roughly like this (only the parts we use):

    <PubmedArticleSet>
      <PubmedArticle>
        <MedlineCitation>
          <PMID>123</PMID>
          <Article>
            <Journal> <Title/> <JournalIssue><PubDate/></JournalIssue> </Journal>
            <ArticleTitle/>  <Abstract><AbstractText/>...</Abstract>  <AuthorList>...</AuthorList>
          </Article>
        </MedlineCitation>
        <PubmedData> <ArticleIdList> <ArticleId IdType="doi"/> ... </ArticleIdList> </PubmedData>
      </PubmedArticle>
    </PubmedArticleSet>
"""

import logging
import re
import xml.etree.ElementTree as ET

# defusedxml is a safe drop-in replacement for the standard XML parser: it refuses
# malicious XML tricks (e.g. "entity expansion" bombs) that could eat all our memory.
from defusedxml import DefusedXmlException
from defusedxml import ElementTree as SafeET

from app.schemas.paper import Paper

logger = logging.getLogger(__name__)

SOURCE = "pubmed"

# {"jan": 1, "feb": 2, ...} used to turn "Mar" into 3.
_MONTHS = {
    name: index
    for index, name in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}
_YEAR = re.compile(r"\b(\d{4})\b")  # a standalone 4-digit number
_WHITESPACE = re.compile(r"\s+")  # runs of spaces/newlines/tabs
_DOI_PREFIX = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)  # "https://doi.org/"


class PubMedParseError(Exception):
    """The efetch payload was not usable XML."""


def parse_pubmed_xml(xml: bytes | str) -> list[Paper]:
    try:
        root = SafeET.fromstring(xml)
    except (ET.ParseError, DefusedXmlException) as exc:
        # Broken XML, or XML that defusedxml refused as unsafe.
        raise PubMedParseError(f"invalid PubMed XML: {type(exc).__name__}") from exc

    papers: list[Paper] = []
    for article in root.findall("PubmedArticle"):
        paper = _parse_article(article)
        if paper is not None:  # records we can't use are skipped, not fatal
            papers.append(paper)
    return papers


def _parse_article(article: ET.Element) -> Paper | None:
    citation = article.find("MedlineCitation")
    pmid = _text(citation.find("PMID")) if citation is not None else None
    title = _text(citation.find("Article/ArticleTitle")) if citation is not None else None
    # Without an ID or a title there is nothing meaningful to show, so skip the record.
    if citation is None or not pmid or not title:
        logger.warning("Skipping PubMed record without PMID or title (pmid=%s)", pmid)
        return None

    return Paper(
        id=f"{SOURCE}:{pmid}",  # e.g. "pubmed:12345678" - always the same for the same paper
        title=title,
        authors=_authors(citation),
        abstract=_abstract(citation),
        # Prefer the full journal name; fall back to the abbreviated one.
        journal=_text(citation.find("Article/Journal/Title"))
        or _text(citation.find("MedlineJournalInfo/MedlineTA")),
        publication_date=_publication_date(citation),
        doi=_doi(article, citation),
        source=SOURCE,
        source_id=pmid,
        url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
    )


def _text(element: ET.Element | None) -> str | None:
    """Flattened text including inline markup (<i>, <sub>...), whitespace-normalized.

    Returns None (not "") when the element is missing or empty, so "no value" is always None.
    """
    if element is None:
        return None
    text = _WHITESPACE.sub(" ", "".join(element.itertext())).strip()
    return text or None


def _authors(citation: ET.Element) -> list[str]:
    authors: list[str] = []
    for author in citation.findall("Article/AuthorList/Author"):
        # Some "authors" are groups (e.g. "Back2Health Consortium") stored in CollectiveName.
        collective = _text(author.find("CollectiveName"))
        if collective:
            authors.append(collective)
            continue
        last = _text(author.find("LastName"))
        if not last:
            continue  # an author entry with no surname is unusable
        first = _text(author.find("ForeName")) or _text(author.find("Initials"))
        authors.append(f"{first} {last}" if first else last)
    return authors


def _abstract(citation: ET.Element) -> str | None:
    # "Structured" abstracts come in labelled parts (BACKGROUND, METHODS, ...). We keep the
    # label in front of each part: "BACKGROUND: text". Plain abstracts have no label.
    sections: list[str] = []
    for node in citation.findall("Article/Abstract/AbstractText"):
        body = _text(node)
        if not body:
            continue
        label = node.get("Label")
        sections.append(f"{label}: {body}" if label else body)
    return "\n".join(sections) if sections else None  # no abstract at all -> None


def _publication_date(citation: ET.Element) -> str | None:
    # Main date first; the electronic publication date is only a fallback.
    result = _date_from(citation.find("Article/Journal/JournalIssue/PubDate"))
    if result is None:
        result = _date_from(citation.find("Article/ArticleDate"))
    return result


def _date_from(node: ET.Element | None) -> str | None:
    """Build YYYY / YYYY-MM / YYYY-MM-DD, never claiming more precision than given.

    If PubMed only knows the year we return "2024", not "2024-01-01" (that would be invented).
    """
    if node is None:
        return None

    year = _text(node.find("Year"))
    if not year:
        # <MedlineDate> is free text like "2024 Jan-Feb"; only the year is reliable.
        match = _YEAR.search(_text(node.find("MedlineDate")) or "")
        return match.group(1) if match else None
    if not year.isdigit() or len(year) != 4:
        return None

    month = _month_number(_text(node.find("Month")))
    if month is None:
        return year  # no (valid) month -> year only
    day = _text(node.find("Day"))
    if day and day.isdigit() and 1 <= int(day) <= 31:
        return f"{year}-{month:02d}-{int(day):02d}"
    return f"{year}-{month:02d}"


def _month_number(value: str | None) -> int | None:
    """"Mar" or "03" -> 3. Anything else (None, "Spring", "13") -> None."""
    if not value:
        return None
    if value.isdigit():
        number = int(value)
        return number if 1 <= number <= 12 else None
    return _MONTHS.get(value[:3].lower())  # seasons like "Spring" resolve to None


def _doi(article: ET.Element, citation: ET.Element) -> str | None:
    # Only the article's own IDs: reference lists also contain ArticleId[@IdType='doi'].
    # (Searching the whole record would wrongly pick up a DOI of a paper this one cites.)
    for node in article.findall("PubmedData/ArticleIdList/ArticleId"):
        if node.get("IdType") == "doi":
            doi = _clean_doi(_text(node))
            if doi:
                return doi
    # Fallback location for the DOI, unless PubMed marked it invalid (ValidYN="N").
    for node in citation.findall("Article/ELocationID"):
        if node.get("EIdType") == "doi" and node.get("ValidYN", "Y") != "N":
            doi = _clean_doi(_text(node))
            if doi:
                return doi
    return None  # many papers genuinely have no DOI; we never guess one


def _clean_doi(value: str | None) -> str | None:
    """Turn "https://doi.org/10.1/abc" into "10.1/abc"."""
    if not value:
        return None
    value = _DOI_PREFIX.sub("", value).strip()
    return value or None
