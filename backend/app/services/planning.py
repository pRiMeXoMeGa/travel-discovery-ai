"""Planning service: thin wrapper around `agents/itinerary.plan_itinerary`.

Exists so WS2's `plan_itinerary` MCP tool (and any future caller outside the
concierge orchestrator) calls the service layer rather than reaching into
`agents/itinerary` directly — matching the pattern the other three service
modules establish.

`agents/orchestrator.py::_run_itinerary` is deliberately NOT changed to call
through here: this workstream's file scope for `orchestrator.py` is limited
to `_fallback_search` and its imports; `_run_itinerary` and the other route
runners are a separate, later task.
"""
from ..agents import itinerary
from ..observability import AgentStep
from ..schemas import StructuredQuery


async def plan_itinerary(
    sq: StructuredQuery, candidates_per_stay: int = 5, step: AgentStep | None = None
) -> dict:
    """Return an itinerary plan dict — see `agents/itinerary.plan_itinerary`
    for the full shape (day-by-day stays, costs, swap-out alternatives)."""
    return await itinerary.plan_itinerary(sq, candidates_per_stay=candidates_per_stay, step=step)
