from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.paper import Paper


class ResearchRequest(BaseModel):
    """What the client sends to POST /research."""

    question: str = Field(min_length=3, max_length=1000)

    model_config = {"str_strip_whitespace": True}  # trim spaces before checking the length


class ProviderStatus(BaseModel):
    """How one provider (e.g. PubMed) did on this request."""

    status: Literal["ok", "error"]
    paper_count: int = 0
    error: str | None = None  # a short, safe message when status == "error"


class ResearchResponse(BaseModel):
    """What POST /research returns."""

    question: str  # the original question, as sent
    queries_used: list[str]  # the cleaned-up search text actually sent to the providers
    papers: list[Paper]  # all providers' papers combined
    provider_status: dict[str, ProviderStatus]  # one entry per provider, e.g. "pubmed", "crossref"
