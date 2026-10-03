from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.paper import Paper


class ResearchRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1000)

    model_config = {"str_strip_whitespace": True}


class ProviderStatus(BaseModel):
    status: Literal["ok", "error"]
    paper_count: int = 0
    error: str | None = None


class ResearchResponse(BaseModel):
    question: str
    queries_used: list[str]
    papers: list[Paper]
    provider_status: dict[str, ProviderStatus]
