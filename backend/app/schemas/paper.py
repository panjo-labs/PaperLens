from pydantic import BaseModel, Field, model_validator


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

    # Provenance: every provider record this paper was built from, e.g.
    # ["pubmed:12345678", "crossref:10.1000/xyz"]. A paper that was never merged lists just
    # itself. It is filled in automatically (see the validator below), so providers don't
    # need to set it, and the deduplicator extends it when it merges duplicates.
    source_ids: list[str] = Field(default_factory=list)

    # Position-independent relevance score set by the ranker (higher = more lexically similar
    # to the research question). None until ranked. This is a simple word-overlap heuristic
    # for ordering results - NOT a measure of scientific relevance or quality.
    rank_score: float | None = None

    @model_validator(mode="after")
    def _fill_default_source_ids(self) -> "Paper":
        if not self.source_ids:
            self.source_ids = [f"{self.source}:{self.source_id}"]
        return self
