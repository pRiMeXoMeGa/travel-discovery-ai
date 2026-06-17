"""Concierge orchestrator: coordinates the four agents and streams steps.

Yields step events as it runs so the API can stream them (SSE) and the UI can
show intermediate agent progress. Each event is a dict; the API serializes it.

NOTE: the agent framework choice (LangGraph / CrewAI / custom) goes here —
document the 3-5 line justification in the README. This skeleton uses a plain
async generator as a placeholder for the chosen framework's run loop.
"""
import uuid
from typing import AsyncIterator

from ..observability import AgentStep, RequestTrace
from ..schemas import ConciergeRequest
from . import intent, itinerary, retrieval, review_intel


async def run_concierge(req: ConciergeRequest) -> AsyncIterator[dict]:
    """Drive the agent pipeline, yielding step events + a final answer.

    Event shapes:
      {"type": "step", "agent": ..., "status": "start|done|error", "data": ...}
      {"type": "token", "text": ...}          # streamed answer tokens
      {"type": "done", "trace": {...}}         # observability summary
    """
    trace = RequestTrace(request_id=str(uuid.uuid4()), query=req.query)

    # 1) Intent -------------------------------------------------------------
    yield {"type": "step", "agent": "intent", "status": "start"}
    # TODO: sq = await intent.parse_intent(req.query); record tokens/latency.
    # trace.add(AgentStep("intent", "done", data=sq.model_dump()))
    # yield {"type": "step", "agent": "intent", "status": "done", "data": sq.model_dump()}

    # 2) Retrieval ----------------------------------------------------------
    # yield {"type": "step", "agent": "retrieval", "status": "start"}
    # candidates = await retrieval.retrieve(sq)
    # yield {"type": "step", "agent": "retrieval", "status": "done", "data": ...}

    # 3) Review intelligence ----------------------------------------------
    # yield {"type": "step", "agent": "review_intel", "status": "start"}
    # synthesis, citations = await review_intel.synthesize(ids, focus=req.query)

    # 4) Itinerary (only when the query asks for a multi-stay plan) ---------
    # if needs_itinerary(sq): ... await itinerary.plan_itinerary(sq)

    # 5) Stream the final grounded answer (tokens) -------------------------
    # async for token in llm.stream_text(answer_prompt): yield {"type": "token", "text": token}

    # Failure handling: on agent error, emit a step with status="error" and
    # degrade gracefully (e.g. fall back to traditional filter results).
    raise NotImplementedError("TODO: wire the four agents")

    yield {"type": "done", "trace": trace.summary()}  # pragma: no cover
