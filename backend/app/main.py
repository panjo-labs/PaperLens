import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.api.research import router as research_router
from app.config import Settings, get_settings
from app.providers.pubmed import PubMedProvider
from app.services.orchestrator import ResearchOrchestrator


def create_app(settings: Settings | None = None) -> FastAPI:
    # httpx logs full request URLs at INFO, which would include the NCBI API key.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved = settings or get_settings()
        async with httpx.AsyncClient(timeout=resolved.http_timeout_seconds) as client:
            app.state.orchestrator = ResearchOrchestrator(
                [PubMedProvider(client, resolved)],
                provider_time_budget=resolved.provider_time_budget_seconds,
            )
            yield

    app = FastAPI(title="Evidence-first Research Assistant", lifespan=lifespan)
    app.include_router(research_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
