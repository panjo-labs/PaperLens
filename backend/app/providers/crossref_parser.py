"""Parse Crossref /works JSON into the common Paper model.

Crossref metadata is submitted by publishers and is often incomplete or oddly formatted, so every
field is read defensively and only populated when Crossref actually provides it.

A Crossref reply looks roughly like this (only the parts we use):

    {"status": "ok",
     "message": {"items": [
        {"DOI": "10.1/abc", "title": ["A title"], "author": [{"given": "A", "family": "B"}],
         "container-title": ["Journal"], "issued": {"date-parts": [[2024, 3]]},
         "abstract": "<jats:p>Text</jats:p>", "URL": "https://doi.org/10.1/abc"}, ...]}}
"""

import html
import logging
import re
from typing import Any

from app.schemas.paper import Paper

logger = logging.getLogger(__name__)

SOURCE = "crossref"

# Tag patterns exclude "<" inside a tag, so an unclosed "<" is abandoned at the next "<" instead of
# scanning to the end of the input: matching stays linear on garbled publisher markup.
_TAG = re.compile(r"<[^<>]*>")  # one HTML/XML tag, e.g. "<i>" or "</jats:p>"
_TAG_SPLIT = re.compile(r"(<[^<>]*>)")  # same, but split() keeps the tags as separate pieces
# Tags that were ENTITY-ENCODED in the source ("&lt;p&gt;"): they only become real "<p>" text after decoding, so a
# second strip is needed. Only well-known tag names are removed, never an arbitrary "<...>" (which would eat
# real text such as "p < 0.05 and q > 3"). Limited to paragraph/line-break/JATS tags (what real records contain);
# inline tags such as <b> that are decoded from entities are left as the text they are.
_ENCODED_TAG = re.compile(r"</?(?:p|br|jats:[a-z-]+)\b[^<>]*>", re.IGNORECASE)
_WHITESPACE = re.compile(r"[ \t\r\f\v]+")  # spaces/tabs (NOT newlines: we use those as separators)
_DOI_PREFIX = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)

# `issued` is Crossref's own "earliest of print/online"; the others are fallbacks.
_DATE_FIELDS = ("issued", "published", "published-print", "published-online")


class CrossrefParseError(Exception):
    """The response did not have the shape of a successful /works listing."""


def parse_crossref_response(payload: Any) -> list[Paper]:
    """`payload` is the already-decoded JSON. Wrong overall shape raises; bad single records are skipped."""
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise CrossrefParseError("response is not a successful Crossref reply")
    message = payload.get("message")
    items = message.get("items") if isinstance(message, dict) else None
    if not isinstance(items, list):
        raise CrossrefParseError("response has no list of works")

    papers: list[Paper] = []
    for item in items:
        try:
            paper = _parse_item(item)
        except (ValueError, TypeError, AttributeError, KeyError):  # incl. pydantic ValidationError
            # One weird record must never throw away the other 19.
            logger.warning("Skipping unparseable Crossref record")
            continue
        if paper is not None:
            papers.append(paper)
    return papers


def _parse_item(item: Any) -> Paper | None:
    if not isinstance(item, dict):
        return None

    doi = _clean_doi(item.get("DOI"))
    if not doi:
        # The DOI is Crossref's record key; without it there is no stable identity to build.
        logger.warning("Skipping Crossref record without a DOI")
        return None
    title = _first_text(item.get("title"))
    if not title:
        logger.warning("Skipping Crossref record without a title (doi=%s)", doi)
        return None

    # Every optional field below becomes None / [] when Crossref doesn't supply it.
    return Paper(
        id=f"{SOURCE}:{doi}",
        title=title,
        authors=_authors(item.get("author")),
        abstract=_abstract(item.get("abstract")),
        journal=_first_text(item.get("container-title")),
        publication_date=_publication_date(item),
        doi=doi,
        source=SOURCE,
        source_id=doi,
        url=_url(item.get("URL")),
    )


def _plain_text(value: Any) -> str | None:
    """Strip markup (<i>, <jats:...>), decode entities (&amp;) and collapse whitespace."""
    if not isinstance(value, str):
        return None
    text = html.unescape(_TAG.sub("", value))  # strip tags first so decoded "&lt;" stays text
    text = _ENCODED_TAG.sub("", text)  # then tags that only appeared after decoding
    text = _WHITESPACE.sub(" ", text).strip()
    return text or None


def _first_text(value: Any) -> str | None:
    """Crossref wraps titles in lists; tolerate a bare string too."""
    candidates = value if isinstance(value, list) else [value]
    for candidate in candidates:
        text = _plain_text(candidate)
        if text:
            return text
    return None


def _authors(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []  # missing or malformed author list -> no authors (never invented)
    authors: list[str] = []
    for author in value:
        if not isinstance(author, dict):
            continue
        name = _plain_text(author.get("name"))  # organisations
        if not name:
            # Persons: "given family". Either part may be missing; use what exists.
            given, family = _plain_text(author.get("given")), _plain_text(author.get("family"))
            name = " ".join(part for part in (given, family) if part) or None
        if name:
            authors.append(name)
    return authors


def _abstract(value: Any) -> str | None:
    """Crossref abstracts are XML-ish text; return clean plain text, or None if there is none."""
    if not isinstance(value, str):
        return None
    # One pass over text and tags (no nested regex): JATS section titles become "Title: " labels
    # and paragraph ends become line breaks; every other tag is dropped.
    # Example: "<jats:title>Methods</jats:title><jats:p>We did X.</jats:p>" -> "Methods: We did X."
    pieces: list[str] = []
    for token in _TAG_SPLIT.split(value):  # alternates: text, tag, text, tag, ...
        if _TAG.fullmatch(token):
            name = token[1:-1].strip().lower().split(maxsplit=1)
            tag = name[0] if name else ""
            if tag == "jats:title":
                pieces.append("\n")  # a section title starts a new line...
            elif tag == "/jats:title":
                pieces.append(": ")  # ...and is followed by a colon
            elif tag == "/jats:p":
                pieces.append("\n")  # end of paragraph
            # any other tag (italic, bold, ...) is simply dropped
        else:
            pieces.append(token)  # ordinary text

    # Clean each line and drop empty ones.
    lines = [_plain_text(line) for line in "".join(pieces).split("\n")]
    lines = [line for line in lines if line and line.strip(": ")]
    if lines and lines[0].lower().rstrip(":") == "abstract":
        lines = lines[1:]  # a bare "Abstract" heading carries no information
    if lines and lines[0].lower().startswith("abstract: "):
        lines[0] = lines[0][len("abstract: ") :]
    return "\n".join(lines) or None


def _publication_date(item: dict[str, Any]) -> str | None:
    # Use the first date field that gives a usable value. "created" (when the record was
    # added to Crossref) is deliberately NOT used: it isn't the publication date.
    for field in _DATE_FIELDS:
        date = _date_parts(item.get(field))
        if date:
            return date
    return None


def _date_parts(value: Any) -> str | None:
    """YYYY / YYYY-MM / YYYY-MM-DD from Crossref date-parts, never claiming more than given.

    Crossref writes dates as {"date-parts": [[2024, 3, 15]]} (year, month, day; the later
    parts may be missing). Unknown dates look like [[null]].
    """
    if not isinstance(value, dict):
        return None
    outer = value.get("date-parts")
    if not isinstance(outer, list) or not outer or not isinstance(outer[0], list):
        return None
    parts = outer[0]

    def number(index: int, low: int, high: int) -> int | None:
        """parts[index] if it's a real integer within [low, high], otherwise None."""
        if index >= len(parts):
            return None
        part = parts[index]
        # bool is a subclass of int in Python, so exclude it explicitly (True is not "year 1").
        if isinstance(part, bool) or not isinstance(part, int) or not low <= part <= high:
            return None
        return part

    year = number(0, 1000, 9999)
    if year is None:
        return None
    month = number(1, 1, 12)
    if month is None:
        return f"{year}"
    day = number(2, 1, 31)
    if day is None:
        return f"{year}-{month:02d}"
    return f"{year}-{month:02d}-{day:02d}"


def _clean_doi(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return _DOI_PREFIX.sub("", value.strip()).strip() or None


def _url(value: Any) -> str | None:
    # Only provider-supplied web links; anything else (e.g. "javascript:") is dropped.
    if isinstance(value, str) and value.strip().lower().startswith(("http://", "https://")):
        return value.strip()
    return None
