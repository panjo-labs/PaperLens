from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration. Values come from environment variables (or a local .env file).

    Field name -> variable name: `ncbi_email` is read from NCBI_EMAIL, `pubmed_retmax` from
    PUBMED_RETMAX, and so on. Secrets are never written in code.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    ncbi_email: str  # required: NCBI asks callers to identify themselves with an email
    ncbi_api_key: SecretStr | None = None  # optional; SecretStr hides it if printed/logged
    ncbi_tool: str = "evidence-first-research-assistant"

    # Field(20, ge=1, le=100) = default 20, and the value must be between 1 and 100.
    pubmed_retmax: int = Field(20, ge=1, le=100)
    http_timeout_seconds: float = Field(10.0, gt=0)  # max seconds for ONE HTTP request
    retry_backoff_seconds: float = 0.5  # pause before the single retry
    # Hard cap on one provider's whole search (all HTTP calls, retries and waits combined).
    provider_time_budget_seconds: float = Field(18.0, gt=0, le=60)

    # Crossref's "polite pool" is selected by sending a contact address; optional, never defaulted.
    crossref_mailto: str | None = None
    crossref_rows: int = Field(20, ge=1, le=100)

    @field_validator("ncbi_api_key", "crossref_mailto", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        # `NCBI_API_KEY=` / `CROSSREF_MAILTO=` in a .env file should mean "not set", not "".
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("ncbi_email", "crossref_mailto")
    @classmethod
    def _email_looks_valid(cls, value: str | None) -> str | None:
        # A very light sanity check (not full email validation): catches typos like "bob.example.com".
        if value is None:
            return None
        value = value.strip()
        if "@" not in value:
            raise ValueError("must be a contact email address")
        return value

    @property
    def pubmed_requests_per_second(self) -> int:
        # NCBI E-utilities limits: 3 req/s without an API key, 10 req/s with one.
        return 10 if self.ncbi_api_key else 3

    @property
    def crossref_requests_per_second(self) -> int:
        # Limits observed in Crossref's x-rate-limit-limit / x-concurrency-limit response headers
        # (2026-10-03): public pool 1 req/s, polite pool (mailto supplied) 3 req/s.
        return 3 if self.crossref_mailto else 1


@lru_cache  # build Settings once and reuse it, instead of re-reading the environment every time
def get_settings() -> Settings:
    return Settings()
