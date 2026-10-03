from typing import Protocol

from app.schemas.paper import Paper


class ProviderError(Exception):
    """A provider failed in an expected way. `message` is safe to show to API clients."""

    def __init__(self, provider: str, message: str) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.message = message


class AcademicSearchProvider(Protocol):
    """The contract every source (PubMed, Crossref, ...) follows.

    A Protocol is "if it has these members, it counts", so providers don't need to inherit
    from anything. The rest of the app only ever talks to this interface.
    """

    name: str  # e.g. "pubmed"

    async def search(self, query: str) -> list[Paper]:
        """Return normalized papers, or raise ProviderError."""
        ...
