from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from app.schemas.research import ResearchRequest, ResearchResponse
from app.services.orchestrator import ResearchOrchestrator
from app.services.query_processor import InvalidQueryError

router = APIRouter()


def get_orchestrator(request: Request) -> ResearchOrchestrator:
    # The orchestrator is created at startup (see main.py). Tests swap it via dependency_overrides.
    return request.app.state.orchestrator


# This layer is deliberately thin: validate the request, call the orchestrator, pick the HTTP
# status. All the real research logic lives in the services/providers layers.
@router.post(
    "/research",
    response_model=ResearchResponse,
    responses={502: {"model": ResearchResponse, "description": "Every provider failed."}},
)
async def research(
    body: ResearchRequest, orchestrator: ResearchOrchestrator = Depends(get_orchestrator)
):
    # FastAPI has already rejected bad bodies (wrong type, too short/long) with a 422.
    try:
        result = await orchestrator.research(body.question)
    except InvalidQueryError as exc:
        # Valid JSON, but nothing searchable in it (e.g. "What is it?").
        raise HTTPException(status_code=422, detail=str(exc)) from None

    statuses = result.provider_status.values()
    if statuses and all(s.status == "error" for s in statuses):
        # Every provider failed -> 502 ("bad gateway") so an outage isn't hidden as an empty 200.
        # Same body shape, so clients can still read per-provider errors.
        return JSONResponse(status_code=502, content=result.model_dump(mode="json"))
    # Success, including "no results" and "one provider failed but another worked".
    return result
