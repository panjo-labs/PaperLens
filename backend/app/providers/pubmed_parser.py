"""Parse PubMed efetch XML into the common Paper model."""

import logging
import re
import xml.etree.ElementTree as ET

from defusedxml import DefusedXmlException
from defusedxml import ElementTree as SafeET

from app.schemas.paper import Paper

logger = logging.getLogger(__name__)

SOURCE = "pubmed"

_MONTHS = {
    name: index
    for index, name in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}
_YEAR = re.compile(r"\b(\d{4})\b")
_WHITESPACE = re.compile(r"\s+")
_DOI_PREFIX = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)


class PubMedParseError(Exception):
    """The efetch payload was not usable XML."""


def parse_pubmed_xml(xml: bytes | str) -> list[Paper]:
    try:
        root = SafeET.fromstring(xml)
    except (ET.ParseError, DefusedXmlException) as exc:
        raise PubMedParseError(f"invalid PubMed XML: {type(exc).__name__}") from exc

    papers: list[Paper] = []
    for article in root.findall("PubmedArticle"):
        paper = _parse_article(article)
        if paper is not None:
            papers.append(paper)
    return papers


def _parse_article(article: ET.Element) -> Paper | None:
    citation = article.find("MedlineCitation")
    pmid = _text(citation.find("PMID")) if citation is not None else None
    title = _text(citation.find("Article/ArticleTitle")) if citation is not None else None
    if citation is None or not pmid or not title:
        logger.warning("Skipping PubMed record without PMID or title (pmid=%s)", pmid)
        return None

    return Paper(
        id=f"{SOURCE}:{pmid}",
        title=title,
        authors=_authors(citation),
        abstract=_abstract(citation),
        journal=_text(citation.find("Article/Journal/Title"))
        or _text(citation.find("MedlineJournalInfo/MedlineTA")),
        publication_date=_publication_date(citation),
        doi=_doi(article, citation),
        source=SOURCE,
        source_id=pmid,
        url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
    )


def _text(element: ET.Element | None) -> str | None:
    """Flattened text including inline markup (<i>, <sub>...), whitespace-normalized."""
    if element is None:
        return None
    text = _WHITESPACE.sub(" ", "".join(element.itertext())).strip()
    return text or None


def _authors(citation: ET.Element) -> list[str]:
    authors: list[str] = []
    for author in citation.findall("Article/AuthorList/Author"):
        collective = _text(author.find("CollectiveName"))
        if collective:
            authors.append(collective)
            continue
        last = _text(author.find("LastName"))
        if not last:
            continue
        first = _text(author.find("ForeName")) or _text(author.find("Initials"))
        authors.append(f"{first} {last}" if first else last)
    return authors


def _abstract(citation: ET.Element) -> str | None:
    sections: list[str] = []
    for node in citation.findall("Article/Abstract/AbstractText"):
        body = _text(node)
        if not body:
            continue
        label = node.get("Label")
        sections.append(f"{label}: {body}" if label else body)
    return "\n".join(sections) if sections else None


def _publication_date(citation: ET.Element) -> str | None:
    result = _date_from(citation.find("Article/Journal/JournalIssue/PubDate"))
    if result is None:
        result = _date_from(citation.find("Article/ArticleDate"))
    return result


def _date_from(node: ET.Element | None) -> str | None:
    """Build YYYY / YYYY-MM / YYYY-MM-DD, never claiming more precision than given."""
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
        return year
    day = _text(node.find("Day"))
    if day and day.isdigit() and 1 <= int(day) <= 31:
        return f"{year}-{month:02d}-{int(day):02d}"
    return f"{year}-{month:02d}"


def _month_number(value: str | None) -> int | None:
    if not value:
        return None
    if value.isdigit():
        number = int(value)
        return number if 1 <= number <= 12 else None
    return _MONTHS.get(value[:3].lower())  # seasons like "Spring" resolve to None


def _doi(article: ET.Element, citation: ET.Element) -> str | None:
    # Only the article's own IDs: reference lists also contain ArticleId[@IdType='doi'].
    for node in article.findall("PubmedData/ArticleIdList/ArticleId"):
        if node.get("IdType") == "doi":
            doi = _clean_doi(_text(node))
            if doi:
                return doi
    for node in citation.findall("Article/ELocationID"):
        if node.get("EIdType") == "doi" and node.get("ValidYN", "Y") != "N":
            doi = _clean_doi(_text(node))
            if doi:
                return doi
    return None


def _clean_doi(value: str | None) -> str | None:
    if not value:
        return None
    value = _DOI_PREFIX.sub("", value).strip()
    return value or None
