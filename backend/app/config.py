from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    ncbi_email: str
    ncbi_api_key: SecretStr | None = None
    ncbi_tool: str = "evidence-first-research-assistant"

    pubmed_retmax: int = Field(20, ge=1, le=100)
    http_timeout_seconds: float = Field(10.0, gt=0)
    # Hard cap on one provider's whole search (all HTTP calls, retries and waits combined).
    provider_time_budget_seconds: float = Field(18.0, gt=0, le=60)
    retry_backoff_seconds: float = 0.5

    @field_validator("ncbi_api_key", mode="before")
    @classmethod
    def _blank_key_is_none(cls, value: object) -> object:
        # `NCBI_API_KEY=` in a .env file should mean "no key", not an empty key.
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("ncbi_email")
    @classmethod
    def _email_looks_valid(cls, value: str) -> str:
        value = value.strip()
        if "@" not in value:
            raise ValueError("NCBI_EMAIL must be a contact email address")
        return value

    @property
    def pubmed_requests_per_second(self) -> int:
        # NCBI E-utilities limits: 3 req/s without an API key, 10 req/s with one.
        return 10 if self.ncbi_api_key else 3


@lru_cache
def get_settings() -> Settings:
    return Settings()
