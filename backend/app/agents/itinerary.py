"""Itinerary agent: multi-day, multi-property plans.

Produces day-by-day stay cards, per-stay + total cost (from the DETERMINISTIC
calendar in availability.py — never an LLM guess), and one-click swap-out
alternatives per stay (brief §2.3). Respects budget + hard constraints
(e.g. "avoid Deira", "near the metro", "one splurge with a view").

Design:
  * The trip is split into segments. We use a lightweight LLM "planner" call
    (structured JSON) to decide HOW MANY stays and the per-stay theme/budget
    split (e.g. one mid-range + one splurge), grounded by the user's intent.
    If the planner fails, we fall back to a single segment for the whole stay.
  * For each segment we reuse the retrieval agent (grounded, real listings) with
    a segment-specific StructuredQuery, then cost the chosen window with the
    deterministic availability function. We NEVER let the LLM choose properties
    or prices — only the segment structure. Property selection + costing is
    deterministic and grounded.
"""
import logging
from datetime import date, timedelta

from .. import llm
from ..availability import is_available_range
from ..observability import AgentStep
from ..schemas import StructuredQuery
from . import retrieval

logger = logging.getLogger(__name__)

_PLANNER_SCHEMA = {
    "segments": (
        "array of objects, one per distinct stay. Each: {"
        "'nights': integer (nights in this stay), "
        "'theme': string (e.g. 'mid-range near metro', 'splurge with a view'), "
        "'vibe': string|null, "
        "'budget_per_night': number|null (split of the total budget for this stay), "
        "'hard_constraints': array of strings (specific to this stay)}"
    )
}

_PLANNER_SYSTEM = (
    "You are a travel itinerary planner. Decide ONLY the STRUCTURE of the trip — "
    "how to split it into distinct stays (segments) — never the specific properties "
    "or prices (those are chosen deterministically downstream).\n"
    "Rules:\n"
    "- The sum of segment nights MUST equal the total nights.\n"
    "- Respect explicit structure: 'one mid-range night and one splurge night' => 2 "
    "segments; a single base => 1 segment. Default to 1 segment unless the request "
    "clearly implies multiple distinct stays.\n"
    "- Split the total budget sensibly across segments via budget_per_night (same "
    "local currency; do not convert).\n"
    "- Carry global constraints (e.g. 'near the centre', 'avoid the airport', a vibe) "
    "into each relevant segment's hard_constraints.\n"
    "- Do not invent property names or prices."
)


def _trip_nights(sq: StructuredQuery, default: int = 4) -> int:
    if sq.check_in and sq.check_out and sq.check_out > sq.check_in:
        return (sq.check_out - sq.check_in).days
    return default


def _trip_start(sq: StructuredQuery) -> date:
    return sq.check_in or (date.today() + timedelta(days=14))


async def _plan_segments(sq: StructuredQuery, total_nights: int, step: AgentStep | None):
    """LLM decides segment structure; deterministic fallback to one segment."""
    prompt = (
        f"Total nights: {total_nights}. City: {sq.city or 'unspecified'}. "
        f"Total budget: {sq.budget_total or 'unspecified'}. "
        f"Per-night budget: {sq.budget_per_night or 'unspecified'}. "
        f"Vibe: {sq.vibe or 'unspecified'}. "
        f"Hard constraints: {sq.hard_constraints}. "
        f"Soft preferences: {sq.soft_preferences}.\n"
        "Produce the segment plan."
    )
    try:
        raw, usage = await llm.complete_json_with_usage(prompt, _PLANNER_SCHEMA, _PLANNER_SYSTEM)
        if step is not None:
            step.input_tokens += usage.input_tokens
            step.output_tokens += usage.output_tokens
        segments = raw.get("segments")
        if isinstance(segments, list) and segments:
            # Reconcile nights to exactly total_nights.
            cleaned = []
            for seg in segments:
                if not isinstance(seg, dict):
                    continue
                cleaned.append(seg)
            if cleaned:
                _reconcile_nights(cleaned, total_nights)
                return cleaned
    except llm.LLMError as exc:
        logger.warning("itinerary: planner LLM failed (%s) — single segment", exc)
        if step is not None:
            step.detail = f"planner_llm_error: {exc}"

    # Fallback: single segment for the whole stay.
    return [
        {
            "nights": total_nights,
            "theme": "comfortable base",
            "vibe": sq.vibe,
            "budget_per_night": sq.budget_per_night,
            "hard_constraints": sq.hard_constraints,
        }
    ]


def _reconcile_nights(segments: list[dict], total: int) -> None:
    """Force the segment nights to sum to `total` (adjust the last segment)."""
    running = 0
    for seg in segments:
        n = int(seg.get("nights") or 1)
        n = max(1, n)
        seg["nights"] = n
        running += n
    diff = total - running
    if diff != 0 and segments:
        segments[-1]["nights"] = max(1, segments[-1]["nights"] + diff)


def _segment_query(sq: StructuredQuery, seg: dict) -> StructuredQuery:
    """Build a per-segment StructuredQuery, merging global + segment constraints."""
    seg_hard = list(seg.get("hard_constraints") or [])
    merged_hard = list(dict.fromkeys([*sq.hard_constraints, *seg_hard]))
    budget = seg.get("budget_per_night")
    if budget is None:
        budget = sq.budget_per_night
    return StructuredQuery(
        city=sq.city,
        party_size=sq.party_size,
        budget_per_night=budget,
        hard_constraints=merged_hard,
        soft_preferences=sq.soft_preferences,
        vibe=seg.get("vibe") or sq.vibe,
    )


async def plan_itinerary(
    sq: StructuredQuery, candidates_per_stay: int = 5, step: AgentStep | None = None
) -> dict:
    """Return an itinerary plan dict.

    Shape:
      {
        "city": str|None,
        "total_nights": int,
        "total_cost": float,
        "currency_note": str,
        "within_budget": bool|None,
        "stays": [ {
            "segment": int, "theme": str, "nights": int,
            "check_in": iso, "check_out": iso,
            "chosen": {listing card + rationale + per-stay cost},
            "alternatives": [ ranked swap-outs ],
            "days": [ "YYYY-MM-DD", ... ]
        } ],
        "notes": [ ... ]   # any degradations / unmet constraints
      }
    """
    total_nights = _trip_nights(sq)
    start = _trip_start(sq)
    segments = await _plan_segments(sq, total_nights, step)

    stays: list[dict] = []
    notes: list[str] = []
    total_cost = 0.0
    cursor = start
    used_ids: set[str] = set()

    for idx, seg in enumerate(segments):
        nights = int(seg["nights"])
        seg_in = cursor
        seg_out = cursor + timedelta(days=nights)
        cursor = seg_out

        seg_sq = _segment_query(sq, seg)
        candidates = await retrieval.retrieve(seg_sq, limit=candidates_per_stay + len(used_ids) + 3)

        # Cost each candidate over the actual window with the deterministic
        # calendar; keep only those available for the whole window. Avoid
        # reusing a property already chosen for an earlier segment.
        priced: list[dict] = []
        for card, rationale in candidates:
            if card.id in used_ids:
                continue
            ok, total_price = is_available_range(card.id, seg_in, seg_out, card.price_per_night)
            if not ok:
                continue
            priced.append(
                {
                    "listing": card.model_dump(mode="json"),
                    "rationale": rationale,
                    "stay_cost": total_price,
                }
            )

        if not priced:
            notes.append(
                f"Segment {idx + 1} ({seg.get('theme', 'stay')}): no available "
                "property matched the constraints for these dates."
            )
            continue

        # Rank: prefer within per-night budget, then by rating, then cost.
        budget_pn = seg_sq.budget_per_night
        def _key(p):
            card = p["listing"]
            over = 0
            if budget_pn is not None and card["price_per_night"] > budget_pn:
                over = 1
            rating = card.get("rating") or 0
            return (over, -rating, p["stay_cost"])

        priced.sort(key=_key)
        chosen = priced[0]
        used_ids.add(chosen["listing"]["id"])
        total_cost += chosen["stay_cost"]

        stays.append(
            {
                "segment": idx + 1,
                "theme": seg.get("theme", "stay"),
                "nights": nights,
                "check_in": seg_in.isoformat(),
                "check_out": seg_out.isoformat(),
                "chosen": chosen,
                "alternatives": priced[1 : candidates_per_stay],
                "days": [(seg_in + timedelta(days=d)).isoformat() for d in range(nights)],
            }
        )

    within_budget: bool | None = None
    if sq.budget_total is not None:
        within_budget = round(total_cost, 2) <= sq.budget_total
        if not within_budget:
            notes.append(
                f"Total {round(total_cost, 2)} exceeds the budget of {sq.budget_total}; "
                "consider the cheaper swap-out alternatives per stay."
            )

    return {
        "city": sq.city,
        "total_nights": total_nights,
        "total_cost": round(total_cost, 2),
        "currency_note": "Prices are in the listing's base currency (deterministic calendar).",
        "within_budget": within_budget,
        "stays": stays,
        "notes": notes,
    }
