from pydantic import BaseModel


class Paper(BaseModel):
    """Provider-independent paper representation used throughout the pipeline."""

    id: str
    title: str
    authors: list[str]
    abstract: str | None = None
    journal: str | None = None
    # Partial dates keep the precision the provider gave: "2024", "2024-03", "2024-03-15".
    publication_date: str | None = None
    doi: str | None = None
    source: str
    source_id: str
    url: str | None = None
