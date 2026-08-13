"""Agent/concierge endpoints with SSE streaming (brief §2.3 / §2.4).

Streams intermediate agent steps + answer tokens so the UI can show progress.
SSE (not WebSocket) keeps it simple and works through Render/Vercel over HTTPS.
"""
import json

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from ..agents import intent as intent_agent
from ..agents.orchestrator import _sq_to_filters, run_concierge
from ..schemas import ConciergeRequest
from ..services import listings as listings_service

router = APIRouter(prefix="/api", tags=["agents"])


@router.post("/concierge/stream")
async def concierge_stream(req: ConciergeRequest) -> EventSourceResponse:
    async def event_gen():
        try:
            async for event in run_concierge(req):
                yield {"event": event.get("type", "message"), "data": json.dumps(event)}
        except Exception as exc:  # graceful failure (brief §3)
            yield {"event": "error", "data": json.dumps({"message": str(exc)})}

    # Disable proxy buffering so Render (and any reverse proxy) streams events
    # incrementally over HTTPS instead of buffering the whole response — the
    # most common SSE-over-HTTPS deploy breakage. sse-starlette already pings
    # to keep the connection alive.
    return EventSourceResponse(
        event_gen(),
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/nl-search")
async def nl_search(req: ConciergeRequest) -> dict:
    """Parse a natural-language query into structured filters AND run the search.

    Returns:
      * understanding: the StructuredQuery (drives filter chips in the UI)
      * filters: the SearchFilters the structured query maps to
      * results: the traditional search results for those filters

    The structured-output Intent agent does the NL->structured step; the rest
    reuses the Phase-2 search logic unchanged, so the two surfaces stay
    consistent and the search endpoint is never regressed.
    """
    sq = await intent_agent.parse_intent(req.query)

    # Carry hard-constraint amenities through to the structured filters so the
    # chips + the actual search agree on what was understood. Both steps go
    # through the service layer (WS0-C) rather than reaching into a sibling
    # router's endpoint function or an agent's private helper directly.
    filters = _sq_to_filters(sq)
    filters = listings_service.apply_constraint_filters(sq, filters)

    response = await listings_service.search_listings(filters)
    return {
        "understanding": sq.model_dump(mode="json"),
        "filters": filters.model_dump(mode="json"),
        "results": response.model_dump(mode="json"),
        # EVAL Q6. This surface has no answer agent, so anything the parse could
        # not represent ("a castle on the moon") was dropped with nothing to say
        # so: the traveller saw two cheap real listings and no hint that most of
        # their request had evaporated. Promoted out of `understanding` to the
        # top level because the UI has to render it, and a field buried in a
        # debug payload is not a disclosure.
        "unsupported": sq.unsupported,
    }
