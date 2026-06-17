"""Agent/concierge endpoints with SSE streaming (brief §2.3 / §2.4).

Streams intermediate agent steps + answer tokens so the UI can show progress.
SSE (not WebSocket) keeps it simple and works through Render/Vercel over HTTPS.
"""
import json

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from ..agents.orchestrator import run_concierge
from ..schemas import ConciergeRequest

router = APIRouter(prefix="/api", tags=["agents"])


@router.post("/concierge/stream")
async def concierge_stream(req: ConciergeRequest) -> EventSourceResponse:
    async def event_gen():
        try:
            async for event in run_concierge(req):
                yield {"event": event.get("type", "message"), "data": json.dumps(event)}
        except Exception as exc:  # graceful failure (brief §3)
            yield {"event": "error", "data": json.dumps({"message": str(exc)})}

    return EventSourceResponse(event_gen())


@router.post("/nl-search")
async def nl_search(req: ConciergeRequest) -> dict:
    """Parse a natural-language query into structured filters for the search bar.

    Returns the StructuredQuery so the frontend can update the filter chips to
    show what was understood, then call /api/search.
    TODO: call agents.intent.parse_intent and map -> SearchFilters.
    """
    raise NotImplementedError("TODO: NL -> filters")
