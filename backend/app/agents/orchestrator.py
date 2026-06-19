"""Concierge orchestrator: coordinates the four agents and streams steps.

Framework choice: a plain async-generator state machine (not LangGraph/CrewAI).
Rationale: the pipeline is a short, mostly-linear route (intent -> {retrieval |
review_intel | itinerary} -> grounded answer) with one branch; a hand-rolled
generator gives us first-class SSE step events, exact token/latency accounting
via RequestTrace, and graceful per-agent degradation with far less weight and
zero extra dependency surface than a framework would add on a free-tier box.

Routing (by intent + keywords):
  * planning / multi-stay  -> intent -> itinerary  (+ grounded narration)
  * review / "is it good"   -> intent -> retrieval(top) -> review_intel
  * default search          -> intent -> retrieval

Every agent runs inside a guard: on error we emit {status:"error"} and degrade
(fall back to traditional filtered results) — the stream never crashes.
"""
import logging
import time
import uuid
from typing import AsyncIterator

from .. import llm
from ..observability import AgentStep, RequestTrace
from ..schemas import ConciergeRequest, SearchFilters, StructuredQuery
from . import intent, itinerary, retrieval, review_intel

logger = logging.getLogger(__name__)

_PLANNING_KEYWORDS = (
    "itinerary", "plan", "nights", "night ", "-night", "day trip", "days",
    "multi", "splurge", "first night", "then move", "two stays", "stay near",
    "route", "week in",
)
_REVIEW_KEYWORDS = (
    "review", "reviews", "consistent", "complain", "praise", "is it good",
    "what do guests", "worth it", "quiet enough", "clean", "noisy",
)


def _classify(query: str, sq: StructuredQuery) -> str:
    q = query.lower()
    if any(k in q for k in _PLANNING_KEYWORDS):
        return "itinerary"
    if any(k in q for k in _REVIEW_KEYWORDS):
        return "review"
    return "search"


def _sq_to_filters(sq: StructuredQuery) -> SearchFilters:
    """Map a StructuredQuery onto the traditional SearchFilters (for fallback)."""
    return SearchFilters(
        city=sq.city,
        check_in=sq.check_in,
        check_out=sq.check_out,
        adults=sq.party_size or 1,
        price_max=sq.budget_per_night,
    )


async def _fallback_search(sq: StructuredQuery) -> list[dict]:
    """Traditional filtered search — graceful degradation when agents fail."""
    from ..routers.search import _build_query, _row_to_card
    from ..db import get_pool

    filters = _sq_to_filters(sq)
    try:
        _, _, rows_sql, rows_params = _build_query(filters)
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(rows_sql, *rows_params)
        cards = []
        for row in rows[:10]:
            card, ok = _row_to_card(row, None, None, None, None)
            if ok:
                cards.append(card.model_dump(mode="json"))
        return cards
    except Exception as exc:  # noqa: BLE001
        logger.error("fallback search failed: %s", exc)
        return []


async def run_concierge(req: ConciergeRequest) -> AsyncIterator[dict]:
    """Drive the agent pipeline, yielding step events + a final answer."""
    trace = RequestTrace(request_id=str(uuid.uuid4()), query=req.query)

    # ── 1) Intent ─────────────────────────────────────────────────────────
    yield {"type": "step", "agent": "intent", "status": "start"}
    step = AgentStep("intent", "start")
    t0 = time.perf_counter()
    sq = StructuredQuery()
    try:
        sq = await intent.parse_intent(req.query, step=step)
        step.status, step.data = "done", sq.model_dump(mode="json")
        step.latency_ms = (time.perf_counter() - t0) * 1000
        trace.add(step)
        yield {"type": "step", "agent": "intent", "status": "done", "data": sq.model_dump(mode="json")}
    except Exception as exc:  # noqa: BLE001
        step.status, step.detail = "error", str(exc)
        step.latency_ms = (time.perf_counter() - t0) * 1000
        trace.add(step)
        yield {"type": "step", "agent": "intent", "status": "error", "data": {"message": str(exc)}}

    route = _classify(req.query, sq)
    yield {"type": "step", "agent": "router", "status": "done", "data": {"route": route}}

    citations: list[dict] = []
    answer_context = ""
    plan: dict | None = None

    # ── 2) Route ──────────────────────────────────────────────────────────
    if route == "itinerary":
        answer_context, citations, plan = await _run_itinerary(req, sq, trace)
        async for ev in _emit_route_events(trace, "itinerary"):
            yield ev
    elif route == "review":
        answer_context, citations = await _run_review(req, sq, trace)
        async for ev in _emit_route_events(trace, "review"):
            yield ev
    else:
        answer_context, citations = await _run_search(req, sq, trace)
        async for ev in _emit_route_events(trace, "search"):
            yield ev

    # Structured itinerary plan (day-by-day stays + costs + swap-out alternatives)
    # so the UI can render real cards rather than re-parsing prose.
    if plan:
        yield {"type": "itinerary", "plan": plan}
    # Surface citations to the client before the answer tokens.
    yield {"type": "data", "citations": citations}

    # ── 3) Stream the grounded answer ─────────────────────────────────────
    yield {"type": "step", "agent": "answer", "status": "start"}
    answer_step = AgentStep("answer", "start")
    t_ans = time.perf_counter()
    streamed = False
    try:
        prompt, system = _answer_prompt(req.query, route, answer_context)
        async for tok in llm.stream_text(prompt, system):
            streamed = True
            answer_step.output_tokens += 1  # coarse token proxy for streaming
            yield {"type": "token", "text": tok}
        answer_step.status = "done"
        # Explicit done so the UI can stop the "Writing answer" spinner.
        yield {"type": "step", "agent": "answer", "status": "done"}
    except llm.LLMError as exc:
        answer_step.status, answer_step.detail = "error", str(exc)
        if not streamed:
            # Degrade: emit the deterministic context as the answer.
            yield {"type": "token", "text": _degraded_answer(route, answer_context)}
        yield {"type": "step", "agent": "answer", "status": "error", "data": {"message": str(exc)}}
    answer_step.latency_ms = (time.perf_counter() - t_ans) * 1000
    trace.add(answer_step)

    # ── 4) Done + trace ───────────────────────────────────────────────────
    summary = trace.summary()
    logger.info("concierge done: %s", summary)
    yield {"type": "done", "trace": summary}


# ── Route runners ─────────────────────────────────────────────────────────────
async def _run_search(req, sq, trace) -> tuple[str, list[dict]]:
    step = AgentStep("retrieval", "start")
    t0 = time.perf_counter()
    try:
        candidates = await retrieval.retrieve(sq, limit=10)
        step.status = "done"
        step.data = {
            "count": len(candidates),
            "top": [{"id": c.id, "name": c.name, "rationale": r} for c, r in candidates[:6]],
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("retrieval failed (%s) — falling back", exc)
        step.status, step.detail = "error", str(exc)
        cards = await _fallback_search(sq)
        step.data = {"fallback": True, "count": len(cards), "top": cards[:6]}
        step.latency_ms = (time.perf_counter() - t0) * 1000
        trace.add(step)
        ctx = _format_candidates_ctx(step.data["top"])
        return ctx, []
    step.latency_ms = (time.perf_counter() - t0) * 1000
    trace.add(step)
    top = [
        {"id": c.id, "name": c.name, "neighbourhood": c.neighbourhood,
         "price_per_night": c.price_per_night, "rating": c.rating, "rationale": r}
        for c, r in candidates[:6]
    ]
    citations = [{"kind": "listing", "id": c.id, "snippet": c.name} for c, _ in candidates[:6]]
    return _format_candidates_ctx(top), citations


async def _run_review(req, sq, trace) -> tuple[str, list[dict]]:
    # First find the property/properties the review question is about.
    rstep = AgentStep("retrieval", "start")
    t0 = time.perf_counter()
    listing_ids: list[str] = []
    listing_names: dict[str, str] = {}
    try:
        candidates = await retrieval.retrieve(sq, limit=3)
        listing_ids = [c.id for c, _ in candidates]
        listing_names = {c.id: c.name for c, _ in candidates}
        rstep.status, rstep.data = "done", {"listing_ids": listing_ids}
    except Exception as exc:  # noqa: BLE001
        rstep.status, rstep.detail = "error", str(exc)
    rstep.latency_ms = (time.perf_counter() - t0) * 1000
    trace.add(rstep)

    sstep = AgentStep("review_intel", "start")
    t1 = time.perf_counter()
    try:
        text, cites = await review_intel.synthesize(listing_ids, focus=req.query, step=sstep)
        sstep.status, sstep.data = "done", {"citations": len(cites)}
        sstep.latency_ms = (time.perf_counter() - t1) * 1000
        trace.add(sstep)
        name_hint = ", ".join(listing_names.values()) or "the property"
        ctx = f"Property: {name_hint}\nGrounded review synthesis:\n{text}"
        return ctx, [c.model_dump() for c in cites]
    except Exception as exc:  # noqa: BLE001
        sstep.status, sstep.detail = "error", str(exc)
        sstep.latency_ms = (time.perf_counter() - t1) * 1000
        trace.add(sstep)
        return "No review evidence could be retrieved.", []


async def _run_itinerary(req, sq, trace) -> tuple[str, list[dict]]:
    step = AgentStep("itinerary", "start")
    t0 = time.perf_counter()
    try:
        plan = await itinerary.plan_itinerary(sq, step=step)
        step.status, step.data = "done", _plan_summary_for_trace(plan)
        step.latency_ms = (time.perf_counter() - t0) * 1000
        trace.add(step)
        citations = []
        for stay in plan.get("stays", []):
            lid = stay["chosen"]["listing"]["id"]
            name = stay["chosen"]["listing"]["name"]
            citations.append({"kind": "listing", "id": lid, "snippet": name})
        return _format_plan_ctx(plan), citations, plan
    except Exception as exc:  # noqa: BLE001
        logger.warning("itinerary failed (%s) — falling back to search", exc)
        step.status, step.detail = "error", str(exc)
        step.latency_ms = (time.perf_counter() - t0) * 1000
        trace.add(step)
        cards = await _fallback_search(sq)
        return _format_candidates_ctx(cards[:6]), [], None


async def _emit_route_events(trace, route) -> AsyncIterator[dict]:
    """Replay the steps recorded during a route runner as SSE step events."""
    for s in trace.steps:
        if s.agent in ("intent", "router", "answer"):
            continue
        yield {"type": "step", "agent": s.agent, "status": s.status, "data": s.data}


# ── Context / prompt builders ─────────────────────────────────────────────────
def _format_candidates_ctx(top: list[dict]) -> str:
    if not top:
        return "No matching properties were found for these constraints."
    lines = []
    for c in top:
        rationale = c.get("rationale", "")
        lines.append(
            f"- {c.get('name')} (id {c.get('id')}): "
            f"{c.get('neighbourhood', '')}, "
            f"${c.get('price_per_night', '?')}/night, rating {c.get('rating', '?')}. "
            f"{rationale}"
        )
    return "Candidate properties (grounded retrieval results):\n" + "\n".join(lines)


def _format_plan_ctx(plan: dict) -> str:
    lines = [
        f"City: {plan.get('city')}. Total nights: {plan.get('total_nights')}. "
        f"Total cost: {plan.get('total_cost')}. Within budget: {plan.get('within_budget')}."
    ]
    for stay in plan.get("stays", []):
        chosen = stay["chosen"]["listing"]
        alts = ", ".join(a["listing"]["name"] for a in stay.get("alternatives", [])[:2])
        lines.append(
            f"Stay {stay['segment']} ({stay['theme']}), {stay['nights']} nights "
            f"{stay['check_in']}->{stay['check_out']}: {chosen['name']} "
            f"({chosen.get('neighbourhood', '')}, ${chosen['price_per_night']}/night, "
            f"stay cost {stay['chosen']['stay_cost']}). "
            f"Swap-out alternatives: {alts or 'none'}."
        )
    for note in plan.get("notes", []):
        lines.append(f"NOTE: {note}")
    return "\n".join(lines)


def _plan_summary_for_trace(plan: dict) -> dict:
    return {
        "stays": len(plan.get("stays", [])),
        "total_cost": plan.get("total_cost"),
        "within_budget": plan.get("within_budget"),
        "notes": plan.get("notes", []),
    }


_ANSWER_SYSTEM = (
    "You are a concise, trustworthy travel concierge. Answer using ONLY the grounded "
    "context provided — never invent property names, prices, neighbourhoods, "
    "amenities, or review claims that are not in the context. Refer to properties by "
    "their exact name and keep prices in the context's currency. For a trip plan, "
    "give a tight stay-by-stay / day-by-day summary with the total cost. If the "
    "context shows nothing matched or evidence is missing, say so plainly and suggest "
    "relaxing one constraint. Be warm but brief — a few short sentences."
)


def _answer_prompt(query: str, route: str, context: str) -> tuple[str, str]:
    prompt = (
        f"User request: {query}\n\n"
        f"GROUNDED CONTEXT ({route}):\n{context}\n\n"
        "Write the concierge response now, grounded strictly in the context above."
    )
    return prompt, _ANSWER_SYSTEM


def _degraded_answer(route: str, context: str) -> str:
    """Used when token streaming is unavailable — return the grounded context."""
    return (
        "I'm having trouble generating a polished answer right now, but here is "
        f"what I found:\n\n{context}"
    )
