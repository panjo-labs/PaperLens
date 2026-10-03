from pydantic import BaseModel


class Paper(BaseModel):
    """Provider-independent paper representation used throughout the pipeline.

    Whatever the source (PubMed, Crossref...), a paper looks like this. A field we don't
    know is None (or an empty list) - we never guess or fill in a made-up value.
    """

    id: str  # "<source>:<source_id>", e.g. "pubmed:12345678" - stable for the same paper
    title: str
    authors: list[str]
    abstract: str | None = None
    journal: str | None = None
    # Partial dates keep the precision the provider gave: "2024", "2024-03", "2024-03-15".
    publication_date: str | None = None
    doi: str | None = None
    source: str  # which provider returned it: "pubmed" or "crossref"
    source_id: str  # the provider's own ID (PMID for PubMed, DOI for Crossref)
    url: str | None = None
