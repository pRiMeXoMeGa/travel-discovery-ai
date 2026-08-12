"""The trip-planner graph (WS3).

Shape — the reason this is a graph and the concierge is not:

    parse ──> plan ──> check_budget ──┬──(over budget, attempts left)──> plan   [CYCLE]
                                      │
                                      └──(ok / out of attempts)──> review
                                                                     │
                                              interrupt() ───────────┤
                                                                     │
                        ┌──(adjust)──> plan  [CYCLE]  <──────────────┤
                        │                                            │
                        └──────────────────> finalize <──(approve)───┘

Two properties LangGraph does not give you for free, both preserved:

* **Per-node SSE step events.** Each node appends to `state["events"]`, which the
  router drains and emits on the existing SSE vocabulary — no new event type,
  same `step`/`token`/`done` contract the frontend already parses.
* **Per-node error guards.** `_guard` wraps every node so a failure records an
  error event and degrades to a usable state rather than killing the stream.
  This matches the contract every agent in `orchestrator.py` already honours.

`RequestTrace` is deliberately NOT part of graph state. Checkpointed state is
replayed on resume, so a trace living inside it would double-count tokens the
moment a thread resumes.
"""
from __future__ import annotations

import logging
from typing import Annotated, Any, Literal, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..schemas import StructuredQuery
from ..services import planning as planning_service

logger = logging.getLogger(__name__)

# How many times the graph may relax constraints and replan before it gives up
# and presents the best it has. Bounded on purpose: an unbounded replan cycle on
# an impossible budget is an infinite loop that bills LLM calls.
MAX_REPLANS = 2


def _append(left: list, right: list) -> list:
    """Reducer so concurrent/looping nodes append rather than overwrite."""
    return (left or []) + (right or [])


class PlannerState(TypedDict, total=False):
    query: str
    user_id: Optional[str]
    trip_id: Optional[str]
    sq: Optional[dict]
    plan: Optional[dict]
    replans: int
    decision: Optional[str]
    relaxed: list
    feedback: Optional[str]
    finalized: bool
    events: Annotated[list, _append]
    errors: Annotated[list, _append]


def _event(node: str, status: str, **data) -> dict:
    return {"agent": node, "status": status, "data": data or None}


async def _parse(state: PlannerState) -> dict:
    from ..agents import intent

    sq = await intent.parse_intent(state["query"])
    return {
        "sq": sq.model_dump(mode="json"),
        "replans": 0,
        "events": [_event("planner_intent", "done", city=sq.city)],
    }


async def _plan(state: PlannerState) -> dict:
    sq = StructuredQuery(**(state.get("sq") or {}))

    # On a replan, relax whichever budget is actually binding. Relaxing only
    # `budget_per_night` looks reasonable and does nothing in the common case:
    # "total budget $6" gives budget_total=6 and budget_per_night=None, so the
    # nightly cap is not what emptied the result set.
    #
    # Relaxing a number the traveller stated is a real product decision, so it
    # is bounded (MAX_REPLANS), proportional, and DISCLOSED — `relaxed` is
    # carried in state so the finalize step can say the budget was stretched
    # rather than quietly overspending. Same principle as WS1's dealbreaker
    # override path.
    replans = state.get("replans", 0)
    relaxed: list[str] = []
    if replans:
        factor = 1 + 0.25 * replans
        if sq.budget_per_night:
            sq.budget_per_night = round(sq.budget_per_night * factor, 2)
            relaxed.append(f"nightly budget +{int(25 * replans)}%")
        if sq.budget_total:
            sq.budget_total = round(sq.budget_total * factor, 2)
            relaxed.append(f"total budget +{int(25 * replans)}%")

    feedback = state.get("feedback")
    if feedback:
        # A human adjustment is a soft preference, never a hard filter — it is
        # free text and cannot be validated against the payload vocabulary.
        sq.soft_preferences = [*sq.soft_preferences, feedback]

    plan = await planning_service.plan_itinerary(sq)
    return {
        "plan": plan,
        "relaxed": relaxed,
        "events": [
            _event(
                "planner_plan", "done",
                stays=len(plan.get("stays") or []),
                total_cost=plan.get("total_cost"),
                replan=replans,
                relaxed=relaxed or None,
            )
        ],
    }


def _needs_replan(plan: dict) -> bool:
    """True when the plan is unusable and relaxing constraints might help.

    Two distinct failures, both worth another attempt:
      * over budget — a plan exists but costs too much;
      * EMPTY — no stays matched at all, which is the more common outcome of an
        unrealistic budget. `within_budget` is None here (see itinerary.py),
        NOT False, so testing only `is False` would skip the case the cycle
        most needs to handle.
    """
    if not (plan.get("stays") or []):
        return True
    return plan.get("within_budget") is False


async def _check_budget(state: PlannerState) -> dict:
    plan = state.get("plan") or {}
    replan = _needs_replan(plan)
    return {
        "events": [
            _event(
                "planner_budget", "done",
                within_budget=plan.get("within_budget"),
                total_cost=plan.get("total_cost"),
                stays=len(plan.get("stays") or []),
                needs_replan=replan,
            )
        ],
        **({"replans": state.get("replans", 0) + 1} if replan else {}),
    }


def _after_budget(state: PlannerState) -> Literal["plan", "review"]:
    """The cycle. Replan while the plan is unusable and attempts remain."""
    if _needs_replan(state.get("plan") or {}) and state.get("replans", 0) <= MAX_REPLANS:
        return "plan"
    return "review"


async def _review(state: PlannerState) -> dict:
    """The human-in-the-loop interrupt.

    `interrupt()` suspends the graph and persists state via the checkpointer.
    The run ends here; the traveller sees the plan and decides. `POST
    /api/planner/resume` supplies a Command(resume=...) and execution continues
    from this point — in a different process, if the instance spun down.
    """
    plan = state.get("plan") or {}
    decision = interrupt(
        {
            "kind": "plan_review",
            "stays": len(plan.get("stays") or []),
            "total_cost": plan.get("total_cost"),
            "within_budget": plan.get("within_budget"),
            "notes": plan.get("notes") or [],
        }
    )

    # Resumed. `decision` is whatever the resume endpoint passed.
    if isinstance(decision, dict):
        action = (decision.get("action") or "approve").lower()
        feedback = decision.get("feedback")
    else:
        action = str(decision or "approve").lower()
        feedback = None

    out: dict[str, Any] = {
        "decision": action,
        "events": [_event("planner_review", "done", action=action)],
    }
    if action == "adjust":
        out["feedback"] = feedback
        out["replans"] = state.get("replans", 0) + 1
    return out


def _after_review(state: PlannerState) -> Literal["plan", "finalize"]:
    if state.get("decision") == "adjust" and state.get("replans", 0) <= MAX_REPLANS:
        return "plan"
    return "finalize"


async def _finalize(state: PlannerState) -> dict:
    plan = state.get("plan") or {}
    relaxed = state.get("relaxed") or []
    if relaxed:
        # Disclose it in the plan the traveller actually reads, not only in a
        # step event they never see.
        plan.setdefault("notes", []).append(
            "To find any option, these were relaxed: " + ", ".join(relaxed) + "."
        )
    return {
        "finalized": True,
        "plan": plan,
        "events": [
            _event(
                "planner_finalize", "done",
                stays=len(plan.get("stays") or []),
                total_cost=plan.get("total_cost"),
                relaxed=relaxed or None,
            )
        ],
    }


def _guard(fn, node: str):
    """Wrap a node so a failure degrades the run instead of killing the stream.

    LangGraph propagates an exception out of `astream`, which on an SSE endpoint
    means the connection dies mid-response and the client sees a truncated
    stream with no explanation. Every agent in the concierge already guards
    itself; the graph nodes must too.
    """
    async def wrapped(state: PlannerState) -> dict:
        try:
            return await fn(state)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "planner node %s failed: %s: %s", node, type(exc).__name__, exc
            )
            return {
                "errors": [f"{node}: {type(exc).__name__}: {exc}"],
                "events": [
                    _event(node, "error", message=f"{type(exc).__name__}: {exc}")
                ],
            }

    wrapped.__name__ = node
    return wrapped


def build_graph(checkpointer=None):
    """Compile the planner graph. `checkpointer` is required for interrupt/resume."""
    g = StateGraph(PlannerState)
    g.add_node("parse", _guard(_parse, "parse"))
    g.add_node("plan", _guard(_plan, "plan"))
    g.add_node("check_budget", _guard(_check_budget, "check_budget"))
    # NOT guarded: _review calls interrupt(), which raises a control-flow
    # exception LangGraph must see. Catching it would silently turn the
    # human-in-the-loop pause into a normal completion.
    g.add_node("review", _review)
    g.add_node("finalize", _guard(_finalize, "finalize"))

    g.add_edge(START, "parse")
    g.add_edge("parse", "plan")
    g.add_edge("plan", "check_budget")
    g.add_conditional_edges("check_budget", _after_budget, {"plan": "plan", "review": "review"})
    g.add_conditional_edges("review", _after_review, {"plan": "plan", "finalize": "finalize"})
    g.add_edge("finalize", END)

    return g.compile(checkpointer=checkpointer)
