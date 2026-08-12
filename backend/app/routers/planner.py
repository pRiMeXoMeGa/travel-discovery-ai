"""HTTP surface for the WS3 LangGraph planner.

    POST /api/planner/stream   run the graph until it finishes OR interrupts
    POST /api/planner/resume   continue an interrupted thread, returns a stream

Both speak the SAME SSE vocabulary the concierge already uses — `step`, `data`,
`done`, `error` — so `frontend/lib/concierge.ts`'s frame parser works unchanged.
One event type is added, and only because nothing existing can express it:

    awaiting_input   terminal; carries `thread_id` and the review payload

That is the whole interrupt/resume contract. A client that receives
`awaiting_input` shows the plan, collects approve/adjust, and POSTs to
/resume with the same `thread_id`.

Why `awaiting_input` rather than reusing `done`: `done` means the turn is over
and the client tears down its stream state. An interrupted run is the opposite
— the turn is suspended and the client must hold `thread_id` to continue it.
Overloading `done` with a "but actually not finished" flag is exactly the kind
of contract that gets missed by one client and silently drops trips.

`langgraph` is imported lazily, like mem0 and fastmcp: it is a heavy optional
dependency and its absence must degrade this router, not break the app.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, AsyncIterator

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/planner", tags=["planner"])


class PlannerRequest(BaseModel):
    query: str
    # Optional: a caller may supply its own id to make retries idempotent.
    thread_id: str | None = None
    user_id: str | None = None
    trip_id: str | None = None


class ResumeRequest(BaseModel):
    thread_id: str
    action: str = Field(default="approve", description="approve | adjust")
    feedback: str | None = None


def _sse(event: dict) -> dict:
    return {"event": event.get("type", "message"), "data": json.dumps(event, default=str)}


def _build():
    """Compile the graph with the Redis checkpointer. Raises if unavailable."""
    from ..planner.checkpointer import RedisCheckpointSaver
    from ..planner.graph import build_graph

    return build_graph(checkpointer=RedisCheckpointSaver())


async def _drain(
    graph, payload: Any, config: dict, seen: int
) -> AsyncIterator[tuple[dict, int]]:
    """Run the graph, yielding one SSE event per node event produced.

    LangGraph's `astream(stream_mode="values")` emits the WHOLE state after each
    node, and `state["events"]` is append-only — so each emission repeats every
    earlier event. `seen` tracks how many have already been sent and only the
    new tail is forwarded; without it the client receives event 1 N times.
    """
    async for state in graph.astream(payload, config, stream_mode="values"):
        events = state.get("events") or []
        for ev in events[seen:]:
            yield (
                {
                    "type": "step",
                    "agent": ev.get("agent"),
                    "status": ev.get("status"),
                    "data": ev.get("data"),
                },
                len(events),
            )
        seen = max(seen, len(events))


async def _run(graph, payload: Any, config: dict, thread_id: str, seen: int = 0):
    """Shared body for stream and resume."""
    last_seen = seen
    try:
        async for event, count in _drain(graph, payload, config, last_seen):
            last_seen = count
            yield _sse(event)

        snapshot = await graph.aget_state(config)
        interrupts = getattr(snapshot, "interrupts", None) or []

        if interrupts:
            # Suspended at the human checkpoint. Terminal for THIS request; the
            # thread lives on in Redis until the traveller decides.
            yield _sse(
                {
                    "type": "awaiting_input",
                    "thread_id": thread_id,
                    "review": interrupts[0].value,
                }
            )
            return

        values = snapshot.values or {}
        plan = values.get("plan")
        if plan:
            yield _sse({"type": "data", "plan": plan})
        yield _sse(
            {
                "type": "done",
                "thread_id": thread_id,
                "finalized": bool(values.get("finalized")),
                "decision": values.get("decision"),
                "errors": values.get("errors") or [],
            }
        )
    except Exception as exc:  # noqa: BLE001
        # Same contract as the concierge: a failure becomes an error frame on
        # the stream, never a truncated response with no explanation.
        logger.warning(
            "planner stream failed (thread=%s): %s: %s",
            thread_id, type(exc).__name__, exc,
        )
        yield _sse({"type": "error", "message": f"{type(exc).__name__}: {exc}"})


def _unavailable(exc: Exception):
    async def gen():
        yield _sse(
            {
                "type": "error",
                "message": "planner unavailable (langgraph not installed)",
                "detail": f"{type(exc).__name__}: {exc}",
            }
        )

    return EventSourceResponse(
        gen(), headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@router.post("/stream")
async def planner_stream(req: PlannerRequest) -> EventSourceResponse:
    """Run the planner until it completes or hits the human checkpoint."""
    try:
        graph = _build()
    except Exception as exc:  # noqa: BLE001
        return _unavailable(exc)

    thread_id = req.thread_id or str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    payload = {
        "query": req.query,
        "user_id": req.user_id,
        "trip_id": req.trip_id,
        "events": [],
        "errors": [],
    }
    return EventSourceResponse(
        _run(graph, payload, config, thread_id),
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/resume")
async def planner_resume(req: ResumeRequest) -> EventSourceResponse:
    """Continue an interrupted thread. Returns a fresh stream of what follows.

    The thread may have been created by a DIFFERENT process — the whole point
    of a Redis checkpointer is that a Render free-tier spin-down between the
    interrupt and the traveller's decision does not lose the trip.
    """
    try:
        graph = _build()
        from langgraph.types import Command
    except Exception as exc:  # noqa: BLE001
        return _unavailable(exc)

    config = {"configurable": {"thread_id": req.thread_id}}

    # Replaying the events already streamed before the interrupt would make the
    # client render the whole plan twice, so start from the count already
    # persisted rather than from zero.
    try:
        snapshot = await graph.aget_state(config)
        seen = len((snapshot.values or {}).get("events") or [])
        known = bool(snapshot.values)
    except Exception:  # noqa: BLE001
        seen, known = 0, False

    if not known:
        async def missing():
            yield _sse(
                {
                    "type": "error",
                    "message": "unknown or expired thread_id",
                    "thread_id": req.thread_id,
                }
            )

        return EventSourceResponse(
            missing(), headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        )

    command = Command(resume={"action": req.action, "feedback": req.feedback})
    return EventSourceResponse(
        _run(graph, command, config, req.thread_id, seen=seen),
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
