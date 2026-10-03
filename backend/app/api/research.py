from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from app.schemas.research import ResearchRequest, ResearchResponse
from app.services.orchestrator import ResearchOrchestrator
from app.services.query_processor import InvalidQueryError

router = APIRouter()


def get_orchestrator(request: Request) -> ResearchOrchestrator:
    return request.app.state.orchestrator


@router.post(
    "/research",
    response_model=ResearchResponse,
    responses={502: {"model": ResearchResponse, "description": "Every provider failed."}},
)
async def research(
    body: ResearchRequest, orchestrator: ResearchOrchestrator = Depends(get_orchestrator)
):
    try:
        result = await orchestrator.research(body.question)
    except InvalidQueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    statuses = result.provider_status.values()
    if statuses and all(s.status == "error" for s in statuses):
        # Same body shape, so clients can still read per-provider errors.
        return JSONResponse(status_code=502, content=result.model_dump(mode="json"))
    return result
