"""Itinerary agent: multi-day, multi-property plans.

Produces day-by-day cards, a total cost, and supports one-click swap-out per
stay (brief §2.3). Respects budget and hard constraints (e.g. "avoid Deira",
"near the metro", "one splurge night with a view").
"""
from ..schemas import StructuredQuery


async def plan_itinerary(sq: StructuredQuery, candidates_per_stay: int = 5) -> dict:
    """Return an itinerary plan.

    TODO:
      1. decompose the trip into stays/segments from the structured query.
      2. retrieve candidates per segment (reuse retrieval agent) honoring
         per-segment constraints (budget split, location, vibe).
      3. assemble a plan that fits the TOTAL budget; compute per-stay + total cost.
      4. return day-by-day cards + swap-out alternatives per stay.
    """
    raise NotImplementedError("TODO: itinerary planning")
