import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.api.research import router as research_router
from app.config import Settings, get_settings
from app.providers.crossref import CrossrefProvider
from app.providers.pubmed import PubMedProvider
from app.services.orchestrator import ResearchOrchestrator


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI app. Tests pass their own `settings`; production reads the environment."""
    # httpx logs full request URLs at INFO, which would include the NCBI API key.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Runs once at startup (before `yield`) and once at shutdown (after it).
        resolved = settings or get_settings()
        # ONE HTTP client shared by all providers: it reuses connections, which is faster.
        # The `async with` closes it cleanly when the app shuts down.
        async with httpx.AsyncClient(timeout=resolved.http_timeout_seconds) as client:
            # To add a new source later, add its provider to this list - nothing else changes.
            app.state.orchestrator = ResearchOrchestrator(
                [PubMedProvider(client, resolved), CrossrefProvider(client, resolved)],
                provider_time_budget=resolved.provider_time_budget_seconds,
            )
            yield

    app = FastAPI(title="Evidence-first Research Assistant", lifespan=lifespan)
    app.include_router(research_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        # Lets Render (or you) check that the server is alive.
        return {"status": "ok"}

    return app


app = create_app()  # the object that `uvicorn app.main:app` runs
