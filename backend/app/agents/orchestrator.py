"""Concierge orchestrator: coordinates the four agents and streams steps.

Framework choice: a plain async-generator state machine (not LangGraph/CrewAI).
Rationale: the pipeline is a short, mostly-linear route (intent -> {retrieval |
review_intel | itinerary} -> grounded answer) with one branch; a hand-rolled
generator gives us first-class SSE step events, exact token/latency accounting
via RequestTrace, and graceful per-agent degradation with far less weight and
zero extra dependency surface than a framework would add on a free-tier box.

Routing (by intent + keywords + structured query, `_classify`):
  * planning / multi-stay  -> intent -> itinerary  (+ grounded narration)
    Requires STRUCTURAL evidence (>=2 planning-keyword hits, or a real
    >=3-night trip paired with a multi-stay phrase) — a lone "nights"/"plan"/
    "days" no longer hijacks the route (EVAL Q2 regression).
  * review / "is it good"   -> intent -> retrieval(top) -> review_intel
  * default search          -> intent -> retrieval
  * COMPOSITE: when the query both asks for a listing recommendation AND
    review language ("...and tell me what guests praise/complain about"),
    `_classify` returns ["search", "review"] and run_concierge runs BOTH
    sub-pipelines, merging their grounded contexts + citations before the
    answer step. Itinerary never composites — a trip plan already carries
    its own grounded listing selection per stay.

Every agent runs inside a guard: on error we emit {status:"error"} and degrade
(fall back to traditional filtered results) — the stream never crashes. A
composite run's route loop also has its own guard so one bad sub-pipeline
never sinks the other's answer.
"""
import logging
import time
import uuid
from typing import AsyncIterator

from .. import llm
from ..config import settings
from ..observability import AgentStep, RequestTrace
from ..schemas import ConciergeRequest, SearchFilters, StructuredQuery
from ..services import listings as listings_service
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
# Phrases that specifically imply MULTIPLE distinct stays/segments — unlike
# "nights"/"days"/"plan"/"route", these can't plausibly describe a one-stay
# search, so a single hit here (paired with an actual >=3-night trip) is
# enough structural evidence on its own. See _has_itinerary_structure.
_MULTI_STAY_PHRASES = (
    "itinerary", "multi", "splurge", "first night", "then move", "two stays",
    "week in",
)
# Explicit "find me a stay" language — independent of review language, this is
# what turns a review-keyword hit into a composite [search, review] run
# instead of dropping the listing recommendation entirely. See
# _has_search_ask.
_SEARCH_ASK_MARKERS = (
    "find", "recommend", "suggest", "book", "somewhere to stay",
    "a place to stay", "an entire place", "help me find",
)


def _nights_between(sq: StructuredQuery) -> int | None:
    """Nights derived exactly like `itinerary._trip_nights`, but returns None
    (never a default) when we don't have two real dates — the classifier must
    not assume a trip length the user never stated."""
    if sq.check_in and sq.check_out and sq.check_out > sq.check_in:
        return (sq.check_out - sq.check_in).days
    return None


def _has_itinerary_structure(q: str, sq: StructuredQuery) -> bool:
    """A *structural* signal, not a lone keyword.

    EVAL Q2: "...for 3 nights under 200 a night..." matched only "nights" (a
    single generic hit) and hijacked the whole query into the itinerary
    route, producing zero review citations. Require either:
      (a) >= 2 distinct planning-keyword hits reinforcing each other, or
      (b) an actual multi-night trip (>= 3 nights, from sq.check_in/
          check_out) paired with a phrase that specifically implies multiple
          stays/segments (_MULTI_STAY_PHRASES).
    A single generic word ("plan", "days", "nights", "route") proves nothing
    on its own.
    """
    hits = [k for k in _PLANNING_KEYWORDS if k in q]
    if len(hits) >= 2:
        return True
    nights = _nights_between(sq)
    if nights is not None and nights >= 3 and any(k in q for k in _MULTI_STAY_PHRASES):
        return True
    return False


def _has_search_ask(q: str, sq: StructuredQuery) -> bool:
    """True when the query also asks for a listing recommendation.

    Checked independently of review language so a composite run can still
    surface listings when a review keyword (e.g. "clean") fires on what is
    really a plain search — the old single-route classifier would have
    dropped the listings entirely in that case. Falls back to the parsed
    StructuredQuery's booking fields (budget/dates/party) when the wording
    itself is terse, since the intent agent only fills those when the user
    actually specified stay constraints.
    """
    if any(k in q for k in _SEARCH_ASK_MARKERS):
        return True
    return bool(
        sq.budget_per_night is not None
        or sq.budget_total is not None
        or sq.check_in is not None
        or sq.check_out is not None
        or sq.party_size is not None
    )


def _classify(query: str, sq: StructuredQuery) -> list[str]:
    """Return an ordered, non-empty list of routes to run.

    1. Itinerary requires structural evidence of a multi-stay trip (see
       _has_itinerary_structure). When it fires, itinerary runs ALONE — a
       full trip plan already carries its own grounded listing selection per
       stay, so it never composites with search/review.
    2. Otherwise, a review keyword and a search ask are independent signals.
       Either alone runs its single pipeline — identical to today's
       behaviour for a plain search or a plain "what do guests say"
       question. Both together run BOTH pipelines (search first, so listing
       evidence grounds the answer before review synthesis is appended);
       run_concierge merges their contexts + citations before the answer
       step. This is the Q2 fix: "find X ... and tell me what guests
       praise/complain about" is two asks, not one.
    """
    q = query.lower()
    if _has_itinerary_structure(q, sq):
        return ["itinerary"]

    review_hit = any(k in q for k in _REVIEW_KEYWORDS)
    search_hit = _has_search_ask(q, sq)

    if review_hit and search_hit:
        return ["search", "review"]
    if review_hit:
        return ["review"]
    return ["search"]


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
    """Traditional filtered search — graceful degradation when agents fail.

    Delegates to the service layer (WS0-C) instead of importing a sibling
    HTTP router's private helpers (`_build_query`/`_row_to_card` used to be
    pulled from `routers/search.py` inside this function body, purely to
    dodge a circular import — `app.services.listings` has no such cycle
    with `agents.orchestrator`, so this is now a normal top-level import).
    """
    filters = _sq_to_filters(sq)
    try:
        cards = await listings_service.fallback_search_candidates(filters, limit=10)
        return [c.model_dump(mode="json") for c in cards]
    except Exception as exc:  # noqa: BLE001
        logger.error("fallback search failed: %s", exc)
        return []


# ── Memory (WS1) ─────────────────────────────────────────────────────────────
# Imported lazily and only when actually used: `app.memory.store` pulls in mem0,
# which costs ~117 MB of imports on a 512 MB box. Nothing that merely imports the
# orchestrator (the tests, the router module) should pay that.
_EMPTY_MEMORIES: dict[str, list[dict]] = {"traveller": [], "trip": []}
_NO_DEALBREAKERS: dict[str, list] = {"must": [], "must_not": [], "unmapped": []}


def _new_dealbreakers(extracted, already_known: dict) -> list[dict]:
    """Keep only rules this turn stated that are not already stored.

    Recalled memories are injected into the intent prompt, so the model happily
    re-reports a standing rule it just read there — meaning every subsequent
    turn would persist another copy of the same dealbreaker. Filtering is
    behaviourally invisible (extract_dealbreakers already dedupes what it
    projects into filters) but it keeps the memory panel readable and stops the
    collection growing without bound.
    """
    seen = {
        (c["field"], c["value"], op)
        for op in ("must", "must_not")
        for c in already_known.get(op, [])
    }
    out: list[dict] = []
    for d in extracted or []:
        row = d.model_dump() if hasattr(d, "model_dump") else dict(d)
        if (row.get("field"), row.get("value"), row.get("op")) in seen:
            continue
        out.append(row)
    return out


def _trip_state(sq: StructuredQuery) -> dict:
    """Derived trip state for the trip memory scope.

    Structured fields straight off the parsed query — NOT a copy of the
    traveller's preferences. The two scopes hold different kinds of content on
    purpose, so they cannot contradict each other and the prompt block does not
    carry the same fact twice.
    """
    raw = sq.model_dump(mode="json")
    return {
        k: raw[k]
        for k in ("city", "check_in", "check_out", "party_size",
                  "budget_per_night", "budget_total")
        if raw.get(k) is not None
    }


def _memory_store():
    """Return the memory store module, or None when memory is unavailable.

    Returning None rather than raising keeps every call site a plain `if`, and
    matches the degradation contract: a concierge turn must work identically
    with memory switched off, mem0 uninstalled, or Qdrant unreachable.
    """
    if not getattr(settings, "memory_enabled", False):
        return None
    try:
        from ..memory import store

        return store
    except Exception as exc:  # noqa: BLE001 — mem0 is an optional dependency
        logger.warning("memory unavailable (%s) — continuing without it", exc)
        return None


async def run_concierge(req: ConciergeRequest) -> AsyncIterator[dict]:
    """Drive the agent pipeline, yielding step events + a final answer."""
    trace = RequestTrace(request_id=str(uuid.uuid4()), query=req.query)

    # ── 0) Recall ─────────────────────────────────────────────────────────
    # Before intent, so remembered preferences can inform the parse. Costs ZERO
    # LLM calls — a local fastembed embed plus one Qdrant query — which is why
    # it sits on the critical path at all.
    store = _memory_store() if req.user_id else None
    memories = _EMPTY_MEMORIES
    dealbreakers = _NO_DEALBREAKERS
    memory_context: str | None = None

    if store is not None:
        yield {"type": "step", "agent": "memory", "status": "start",
               "data": {"phase": "recall"}}
        mem_step = AgentStep("memory", "start")
        t_mem = time.perf_counter()
        memories = await store.recall(
            req.query, user_id=req.user_id, trip_id=req.trip_id
        )
        dealbreakers = store.extract_dealbreakers(memories)
        memory_context = store.as_prompt_context(memories) or None
        mem_step.status = "done"
        mem_step.data = {
            "phase": "recall",
            "traveller": memories.get("traveller", []),
            "trip": memories.get("trip", []),
            # Rules the traveller believes are enforced but which have no
            # payload field. Surfaced so the panel can badge them as soft
            # rather than implying a guarantee that does not exist.
            "unmapped": dealbreakers.get("unmapped", []),
        }
        mem_step.latency_ms = (time.perf_counter() - t_mem) * 1000
        trace.add(mem_step)
        yield {"type": "step", "agent": "memory", "status": "done",
               "data": mem_step.data}

    # ── 1) Intent ─────────────────────────────────────────────────────────
    yield {"type": "step", "agent": "intent", "status": "start"}
    step = AgentStep("intent", "start")
    t0 = time.perf_counter()
    sq = StructuredQuery()
    try:
        # Same call count as before memory — context is added to a call that
        # already happens, so recall+injection costs nothing extra.
        sq = await intent.parse_intent(
            req.query, step=step, memory_context=memory_context
        )
        step.status, step.data = "done", sq.model_dump(mode="json")
        step.latency_ms = (time.perf_counter() - t0) * 1000
        trace.add(step)
        yield {"type": "step", "agent": "intent", "status": "done", "data": sq.model_dump(mode="json")}
    except Exception as exc:  # noqa: BLE001
        step.status, step.detail = "error", str(exc)
        step.latency_ms = (time.perf_counter() - t0) * 1000
        trace.add(step)
        yield {"type": "step", "agent": "intent", "status": "error", "data": {"message": str(exc)}}

    routes = _classify(req.query, sq) or ["search"]
    primary_route = routes[0]
    yield {
        "type": "step",
        "agent": "router",
        "status": "done",
        # `route` stays a bare string for backwards compatibility (the
        # frontend reads it); `routes` is additive so a composite run is
        # visible to clients that care.
        "data": {"route": primary_route, "routes": routes},
    }

    citations: list[dict] = []
    answer_context_parts: list[str] = []
    plan: dict | None = None

    # ── 2) Route(s) ───────────────────────────────────────────────────────
    # A per-run cursor into trace.steps replaces the old "replay everything"
    # bug: _emit_route_events used to iterate trace.steps from index 0 on
    # every call, so a second composite run re-emitted the first run's step
    # events verbatim. Slicing [cursor:] after each runner appends its own
    # steps guarantees each SSE step is emitted exactly once.
    # Validated dealbreakers become HARD Qdrant payload conditions, not prompt
    # hints. A hint is a suggestion the model may ignore; a payload filter is a
    # guarantee — which is what "never show me this again" means.
    exclude = dealbreakers if (dealbreakers["must"] or dealbreakers["must_not"]) else None

    for route in routes:
        cursor = len(trace.steps)
        try:
            if route == "itinerary":
                ctx, cites, plan = await _run_itinerary(req, sq, trace, exclude)
            elif route == "review":
                ctx, cites = await _run_review(req, sq, trace, exclude)
            else:
                ctx, cites = await _run_search(req, sq, trace, exclude)
        except Exception as exc:  # noqa: BLE001
            # Belt-and-suspenders: _run_search/_run_review/_run_itinerary
            # already guard their own LLM/DB calls internally and degrade
            # gracefully, but a composite run must never let one unexpected
            # bug in a route sink the other route's answer.
            logger.error("route %r crashed unexpectedly (%s) — degrading", route, exc)
            ctx, cites = f"The {route} step could not be completed.", []
        answer_context_parts.append(ctx)
        citations.extend(cites)
        async for ev in _emit_route_events(trace.steps[cursor:]):
            yield ev

    citations = _merge_citations(citations)
    answer_context = "\n\n".join(p for p in answer_context_parts if p) or (
        "No grounded context is available."
    )
    route_label = "+".join(routes)

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
    chunk_count = 0
    usage_stream: llm.TextStream | None = None
    answer_parts: list[str] = []
    try:
        # `route_label` rather than `route`: after the loop above, `route` holds
        # whichever route happened to run LAST, which on a composite run
        # mislabels the context block (a retrieval+review run would announce
        # itself as just "review"). The joined label is also what the trace
        # records, so prompt and trace agree.
        prompt, system = _answer_prompt(req.query, route_label, answer_context)
        usage_stream = llm.stream_text_with_usage(prompt, system)
        async for tok in usage_stream:
            streamed = True
            chunk_count += 1
            # Accumulated for the memory write below — the extraction needs the
            # assistant turn, and this is the only place the full text exists.
            answer_parts.append(tok)
            yield {"type": "token", "text": tok}
        answer_step.status = "done"
        # Explicit done so the UI can stop the "Writing answer" spinner.
        yield {"type": "step", "agent": "answer", "status": "done"}
    except llm.LLMError as exc:
        answer_step.status, answer_step.detail = "error", str(exc)
        if not streamed:
            # Degrade: emit the deterministic context as the answer.
            yield {"type": "token", "text": _degraded_answer(route_label, answer_context)}
        yield {"type": "step", "agent": "answer", "status": "error", "data": {"message": str(exc)}}

    # Prefer real provider-reported usage (Gemini usageMetadata / Anthropic
    # message_start+message_delta usage) over the coarse per-chunk proxy;
    # fall back to the proxy — rather than reporting zero — whenever usage
    # was unavailable (older frame shape, provider without it, or the stream
    # failed before any usage frame arrived), and mark which one this is so
    # a benchmark can tell measured from estimated.
    if usage_stream is not None and usage_stream.measured:
        answer_step.input_tokens = usage_stream.usage.input_tokens
        answer_step.output_tokens = usage_stream.usage.output_tokens
        answer_step.data = {"usage_source": "measured"}
    else:
        answer_step.output_tokens = chunk_count
        answer_step.data = {"usage_source": "estimated"}

    answer_step.latency_ms = (time.perf_counter() - t_ans) * 1000
    trace.add(answer_step)

    # ── 3b) Write memory ──────────────────────────────────────────────────
    # AFTER the answer has fully streamed: the write costs 1-2 Gemini calls, and
    # the user already has their result, so this adds no perceived latency to
    # the answer itself.
    #
    # Awaited rather than fire-and-forget, deliberately: the panel can only show
    # what was extracted if we wait for it, and an invisible memory layer is
    # indistinguishable from no memory layer. store.remember caps itself (15s,
    # measured against real 3.8-7.9s writes) and swallows its own failures, so a
    # hung write cannot stall the `done` event below.
    if store is not None and answer_parts:
        yield {"type": "step", "agent": "memory", "status": "start",
               "data": {"phase": "write"}}
        w_step = AgentStep("memory", "start")
        t_w = time.perf_counter()
        # Dealbreakers captured by the intent step this turn are persisted with
        # their {field, value, op} metadata. This is the link that makes the
        # feature work: mem0's own extraction produces free text with no
        # metadata, and extract_dealbreakers() matches on that metadata — so
        # without passing them here, a saved rule can never become a filter.
        # Revoke first, then write. Order matters: if the traveller both
        # revokes an old rule and states a new one in the same breath, doing it
        # the other way round could delete the rule just written.
        revoked: list[str] = []
        if sq.suppress_dealbreakers:
            revoked = await store.revoke_dealbreakers(
                req.user_id, sq.suppress_dealbreakers
            )

        written = await store.remember(
            req.query, "".join(answer_parts),
            user_id=req.user_id, trip_id=req.trip_id,
            dealbreakers=_new_dealbreakers(sq.dealbreakers, dealbreakers),
            trip_state=_trip_state(sq) if req.trip_id else None,
        )
        w_step.status = "done"
        # `revoked` is surfaced so the panel can show a rule disappearing as a
        # consequence of what was said, rather than it silently vanishing.
        w_step.data = {"phase": "write", "written": written, "revoked": revoked}
        w_step.latency_ms = (time.perf_counter() - t_w) * 1000
        trace.add(w_step)
        yield {"type": "step", "agent": "memory", "status": "done",
               "data": w_step.data}

    # ── 4) Done + trace ───────────────────────────────────────────────────
    # Routing is recorded on the trace too, not just the transient router step:
    # WS5 benchmarks read the trace, and "which pipelines actually ran" is the
    # thing EVAL Q2 regressed on.
    summary = trace.summary() | {"route": primary_route, "routes": routes}
    logger.info("concierge done: %s", summary)
    yield {"type": "done", "trace": summary}


# ── Route runners ─────────────────────────────────────────────────────────────
async def _retrieve_with_override(
    sq: StructuredQuery, limit: int, exclude: dict | None
) -> tuple[list, bool]:
    """Retrieve, relaxing dealbreakers only if they emptied the result set.

    The override path. A standing rule that silently returns nothing is a worse
    experience than no rule at all: the traveller sees an empty page with no
    cause, and the rule they set months ago is invisible. So when dealbreakers
    are in force AND the constrained search finds nothing, retry unconstrained
    and report that it happened — the caller puts it in the context so the
    answer discloses it rather than quietly pretending the filter never applied.

    Returns (candidates, relaxed).
    """
    candidates = await retrieval.retrieve(sq, limit=limit, exclude=exclude)
    if candidates or not exclude:
        return candidates, False
    relaxed = await retrieval.retrieve(sq, limit=limit, exclude=None)
    return relaxed, bool(relaxed)


def _dealbreaker_note(dealbreakers: dict | None, relaxed: bool) -> str:
    """Context line telling the answer agent what memory did to this search."""
    if not dealbreakers:
        return ""
    rules = [f"{c['field']}={c['value']}" for c in dealbreakers.get("must", [])]
    rules += [f"NOT {c['field']}={c['value']}" for c in dealbreakers.get("must_not", [])]
    if not rules:
        return ""
    if relaxed:
        return (
            "\n\nNOTE: your saved rules (" + ", ".join(rules) + ") matched nothing, "
            "so they were relaxed for this search. Tell the user this happened."
        )
    return (
        "\n\nNOTE: these results were filtered by your saved rules ("
        + ", ".join(rules) + "). Mention that saved preferences were applied."
    )


async def _run_search(req, sq, trace, exclude=None) -> tuple[str, list[dict]]:
    step = AgentStep("retrieval", "start")
    t0 = time.perf_counter()
    relaxed = False
    try:
        candidates, relaxed = await _retrieve_with_override(sq, 10, exclude)
        step.status = "done"
        step.data = {
            "count": len(candidates),
            "dealbreakers_applied": bool(exclude),
            "dealbreakers_relaxed": relaxed,
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
    return _format_candidates_ctx(top) + _dealbreaker_note(exclude, relaxed), citations


async def _run_review(req, sq, trace, exclude=None) -> tuple[str, list[dict]]:
    # First find the property/properties the review question is about.
    rstep = AgentStep("retrieval", "start")
    t0 = time.perf_counter()
    listing_ids: list[str] = []
    listing_names: dict[str, str] = {}
    try:
        candidates, _relaxed = await _retrieve_with_override(sq, 3, exclude)
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


async def _run_itinerary(
    req, sq, trace, exclude=None
) -> tuple[str, list[dict], dict | None]:
    step = AgentStep("itinerary", "start")
    t0 = time.perf_counter()
    try:
        # Dealbreakers constrain trip plans too. itinerary.py threads `exclude`
        # into its single retrieval call, which produces both the chosen stay
        # and its swap-out alternatives — so a traveller cannot click a swap and
        # land on exactly the thing they banned.
        # `trace` is passed so the WS2 weather MCP call lands on the request
        # trace as its own `weather_mcp` step and is therefore visible in the
        # SSE stream. Without it the forecast still reaches plan["notes"], but
        # the external tool call is invisible — and an invisible integration is
        # indistinguishable from no integration.
        plan = await itinerary.plan_itinerary(
            sq, step=step, exclude=exclude, trace=trace
        )
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
        # Log the type as well as the message: several qdrant/httpx transport
        # errors stringify to "", so `itinerary failed () — falling back` was
        # the entire record of a real failure. Same fix as _search_summaries.
        logger.warning(
            "itinerary failed (%s: %s) — falling back to search",
            type(exc).__name__, exc,
        )
        step.status, step.detail = "error", str(exc)
        step.latency_ms = (time.perf_counter() - t0) * 1000
        trace.add(step)
        cards = await _fallback_search(sq)
        return _format_candidates_ctx(cards[:6]), [], None


def _merge_citations(citations: list[dict]) -> list[dict]:
    """De-duplicate citations across a composite run, preserving first order.

    A composite query runs two pipelines that can legitimately cite the same
    listing — retrieval surfaces it as a candidate and review_intel cites it
    again as the property under discussion. Without this the client renders the
    same card twice, and the `[r#]` labels in the answer stop lining up with
    the citation list. Order is preserved so the first pipeline's ranking wins.
    """
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for c in citations:
        key = (str(c.get("kind", "")), str(c.get("id", "")))
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


async def _emit_route_events(steps: list[AgentStep]) -> AsyncIterator[dict]:
    """Replay the steps recorded by ONE route runner as SSE step events.

    Takes just that runner's slice of `trace.steps`, not the whole trace. The
    previous signature took the trace and iterated it from index 0, so on a
    composite run the second runner re-emitted every step the first had already
    emitted — the client saw `retrieval:done` twice and a duplicated trail. The
    caller slices with a cursor captured before each runner, which guarantees
    each step is emitted exactly once.
    """
    for s in steps:
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
    "relaxing one constraint. Be warm but brief — a few short sentences.\n"
    # Disclosure (WS1). Personalisation the user cannot see is indistinguishable
    # from results being wrong: if a saved rule quietly removed every shared
    # room, the traveller needs to know that happened and that they can undo it.
    "If the context contains a NOTE about saved rules or preferences, tell the "
    "user plainly that their saved preferences shaped these results — and if "
    "the note says the rules were relaxed because nothing matched, say that "
    "too. Never present remembered preferences as facts about the properties."
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
