from typing import Protocol

from app.schemas.paper import Paper


class ProviderError(Exception):
    """A provider failed in an expected way. `message` is safe to show to API clients."""

    def __init__(self, provider: str, message: str) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.message = message


class AcademicSearchProvider(Protocol):
    name: str

    async def search(self, query: str) -> list[Paper]:
        """Return normalized papers, or raise ProviderError."""
        ...
