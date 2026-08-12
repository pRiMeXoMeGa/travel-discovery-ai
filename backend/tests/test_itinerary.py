"""Unit tests for the itinerary agent — WS1 dealbreakers as HARD retrieval
filters (brief: a saved "never show me shared rooms" rule must constrain a
trip PLAN, not just search/review, and must hold for swap-out alternatives
too, not just the chosen stay).

Relies on `backend/tests/conftest.py` for dummy provider keys and the
no-network guard. Both `retrieval.retrieve` and the planner LLM
(`itinerary.llm.complete_json_with_usage`) are mocked — zero network, zero
LLM quota. `is_available_range` is monkeypatched to a deterministic stub so
tests do not depend on the real hash-based availability calendar.

Async coroutines are driven with `asyncio.run` so this file does not depend
on pytest-asyncio being configured (matches test_retrieval.py's convention).
"""
import asyncio
import sys
from datetime import date
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app import llm  # noqa: E402
from app.agents import itinerary, retrieval  # noqa: E402
from app.schemas import ListingCard, StructuredQuery  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _card(listing_id: str, price: float = 100.0, rating: float = 4.5) -> ListingCard:
    return ListingCard(
        id=listing_id,
        name=f"Listing {listing_id}",
        type="Entire home/apt",
        city="Lisbon",
        neighbourhood="Santa Maria Maior",
        lat=38.71,
        lng=-9.13,
        price_per_night=price,
        rating=rating,
        review_count=10,
        key_amenities=["wifi"],
        photo=None,
    )


def _sq(**over) -> StructuredQuery:
    base = dict(city="Lisbon", check_in=date(2026, 9, 1), check_out=date(2026, 9, 3))
    base.update(over)
    return StructuredQuery(**base)


def _force_single_segment(monkeypatch):
    """Make `_plan_segments` take its deterministic fallback (1 segment) so
    tests don't depend on parsing a fake LLM segment-structure payload."""

    async def fake_complete_json_with_usage(prompt, schema, system):
        raise llm.LLMError("forced failure for test determinism")

    monkeypatch.setattr(itinerary.llm, "complete_json_with_usage", fake_complete_json_with_usage)


def _force_two_segments(monkeypatch, total_nights: int):
    """Make `_plan_segments` succeed with two segments summing to total_nights."""

    async def fake_complete_json_with_usage(prompt, schema, system):
        payload = {
            "segments": [
                {"nights": 1, "theme": "mid-range", "budget_per_night": None,
                 "hard_constraints": []},
                {"nights": max(1, total_nights - 1), "theme": "splurge",
                 "budget_per_night": None, "hard_constraints": []},
            ]
        }
        return payload, llm.Usage(input_tokens=10, output_tokens=5)

    monkeypatch.setattr(itinerary.llm, "complete_json_with_usage", fake_complete_json_with_usage)


def _always_available(monkeypatch):
    """Deterministic stand-in for availability.is_available_range: every
    candidate is available, priced at price_per_night * nights."""

    def fake_is_available_range(listing_id, check_in, check_out, base_price):
        nights = (check_out - check_in).days
        return True, round(base_price * nights, 2)

    monkeypatch.setattr(itinerary, "is_available_range", fake_is_available_range)


def _wire_retrieve(monkeypatch, cards_by_call=None, default_cards=None):
    """Wire `itinerary.retrieval.retrieve` to a fake that records every call's
    kwargs and returns either the next queued candidate list or `default_cards`.
    """
    calls: list[dict] = []
    queue = list(cards_by_call or [])

    async def fake_retrieve(sq, limit=20, exclude=None):
        calls.append({"sq": sq, "limit": limit, "exclude": exclude})
        if queue:
            cards = queue.pop(0)
        else:
            cards = default_cards if default_cards is not None else []
        return [(card, f"rationale for {card.id}") for card in cards]

    monkeypatch.setattr(itinerary.retrieval, "retrieve", fake_retrieve)
    return calls


_EXCLUDE = {
    "must": [],
    "must_not": [{"field": "type", "value": "Shared room"}],
    "unmapped": [],
}


# ═════════════════════════════════════════════════════════════════════════════
# exclude reaches retrieval for the chosen stay
# ═════════════════════════════════════════════════════════════════════════════
def test_exclude_reaches_retrieval_for_chosen_stay(monkeypatch):
    _force_single_segment(monkeypatch)
    _always_available(monkeypatch)
    calls = _wire_retrieve(monkeypatch, default_cards=[_card("a"), _card("b")])

    plan = _run(itinerary.plan_itinerary(_sq(), exclude=_EXCLUDE))

    assert len(calls) == 1
    assert calls[0]["exclude"] == _EXCLUDE
    assert plan["stays"][0]["chosen"]["listing"]["id"] == "a"


# ═════════════════════════════════════════════════════════════════════════════
# exclude reaches retrieval for the swap-out alternatives — the easy one to
# miss, since `chosen` and `alternatives` are both slices of the SAME
# retrieve() call's results. Cover a multi-segment plan too, so a regression
# that adds a second, exclude-blind call site (e.g. "fetch more alternatives
# separately") would be caught.
# ═════════════════════════════════════════════════════════════════════════════
def test_exclude_reaches_retrieval_for_swap_alternatives(monkeypatch):
    _force_single_segment(monkeypatch)
    _always_available(monkeypatch)
    cards = [_card("a", price=100), _card("b", price=90), _card("c", price=80)]
    calls = _wire_retrieve(monkeypatch, default_cards=cards)

    plan = _run(itinerary.plan_itinerary(_sq(), candidates_per_stay=5, exclude=_EXCLUDE))

    stay = plan["stays"][0]
    assert len(stay["alternatives"]) >= 1
    # Every retrieve() call (the only call site that produces both `chosen`
    # and `alternatives`) must have carried the dealbreaker.
    assert all(c["exclude"] == _EXCLUDE for c in calls)


def test_exclude_reaches_every_segment_including_alternatives(monkeypatch):
    """Multi-segment plan: exclude must reach EACH segment's retrieve() call,
    not just the first."""
    _force_two_segments(monkeypatch, total_nights=4)
    _always_available(monkeypatch)
    seg1_cards = [_card("s1a", price=100), _card("s1b", price=90)]
    seg2_cards = [_card("s2a", price=200), _card("s2b", price=180)]
    calls = _wire_retrieve(monkeypatch, cards_by_call=[seg1_cards, seg2_cards])

    plan = _run(itinerary.plan_itinerary(_sq(), exclude=_EXCLUDE))

    assert len(calls) == 2
    assert all(c["exclude"] == _EXCLUDE for c in calls)
    assert len(plan["stays"]) == 2
    assert plan["stays"][0]["alternatives"]
    assert plan["stays"][1]["alternatives"]


# ═════════════════════════════════════════════════════════════════════════════
# exclude=None (every pre-WS1 caller) reproduces today's calls byte-for-byte
# ═════════════════════════════════════════════════════════════════════════════
def test_exclude_none_is_backwards_compatible(monkeypatch):
    _force_single_segment(monkeypatch)
    _always_available(monkeypatch)
    calls_default = _wire_retrieve(monkeypatch, default_cards=[_card("a")])
    _run(itinerary.plan_itinerary(_sq()))  # no exclude kwarg at all

    assert len(calls_default) == 1
    assert calls_default[0]["exclude"] is None

    calls_explicit = _wire_retrieve(monkeypatch, default_cards=[_card("a")])
    _run(itinerary.plan_itinerary(_sq(), exclude=None))  # explicit None

    assert len(calls_explicit) == 1
    assert calls_explicit[0]["exclude"] is None
    # Same limit/city semantics regardless of whether exclude was passed.
    assert calls_default[0]["limit"] == calls_explicit[0]["limit"]


# ═════════════════════════════════════════════════════════════════════════════
# Empty constrained retrieval degrades gracefully: plan is still produced,
# and the degradation is visible in `notes` rather than silently applied.
# ═════════════════════════════════════════════════════════════════════════════
def test_empty_constrained_retrieval_still_yields_plan_with_note(monkeypatch):
    _force_single_segment(monkeypatch)
    _always_available(monkeypatch)
    calls = _wire_retrieve(monkeypatch, default_cards=[])  # constrained search: nothing

    plan = _run(itinerary.plan_itinerary(_sq(), exclude=_EXCLUDE))

    assert len(calls) == 1
    assert calls[0]["exclude"] == _EXCLUDE
    assert plan["stays"] == []  # segment dropped, not a crash
    assert len(plan["notes"]) == 1
    assert "Segment 1" in plan["notes"][0]
    assert "dealbreaker" in plan["notes"][0].lower()
    # The plan dict itself is still well-formed / SSE-stream-safe.
    assert plan["total_cost"] == 0.0
    assert plan["city"] == "Lisbon"


def test_empty_retrieval_without_exclude_has_no_dealbreaker_note(monkeypatch):
    """Same empty-segment path, but with no exclude in force — the note must
    not falsely claim a dealbreaker was enforced."""
    _force_single_segment(monkeypatch)
    _always_available(monkeypatch)
    _wire_retrieve(monkeypatch, default_cards=[])

    plan = _run(itinerary.plan_itinerary(_sq()))

    assert plan["stays"] == []
    assert len(plan["notes"]) == 1
    assert "dealbreaker" not in plan["notes"][0].lower()


# ═════════════════════════════════════════════════════════════════════════════
# Grounding: property selection stays deterministic (availability + ranking),
# never the LLM, regardless of exclude.
# ═════════════════════════════════════════════════════════════════════════════
def test_chosen_property_is_the_cheapest_within_budget_not_llm_chosen(monkeypatch):
    _force_single_segment(monkeypatch)
    _always_available(monkeypatch)
    cards = [_card("expensive", price=500, rating=5.0), _card("cheap", price=50, rating=3.0)]
    _wire_retrieve(monkeypatch, default_cards=cards)

    plan = _run(
        itinerary.plan_itinerary(_sq(budget_per_night=100), exclude=_EXCLUDE)
    )

    # budget_per_night=100: "expensive" (500) is over budget, "cheap" (50) is not
    # -> deterministic ranking picks "cheap" regardless of its lower rating.
    assert plan["stays"][0]["chosen"]["listing"]["id"] == "cheap"


# ── per-segment area scoping (production defect, 2026-08-12) ─────────────────

def test_segment_keeps_its_own_area_and_drops_the_competing_global():
    """The regression that mattered.

    "one stay near the beach and one near Downtown" puts BOTH phrases in the
    global hard_constraints, and the planner also distributes one per segment.
    Merging globals unconditionally gave every segment both areas, so each
    searched Downtown ∪ beach and which surfaced was ranking luck — observed in
    production as a "beach stay" segment returning a Downtown property.
    """
    sq = StructuredQuery(
        city="Los Angeles",
        hard_constraints=["near the beach", "near Downtown", "wifi"],
    )
    beach = itinerary._segment_query(sq, {"hard_constraints": ["near the beach"]})
    downtown = itinerary._segment_query(sq, {"hard_constraints": ["near Downtown"]})

    assert "near the beach" in beach.hard_constraints
    assert "near Downtown" not in beach.hard_constraints

    assert "near Downtown" in downtown.hard_constraints
    assert "near the beach" not in downtown.hard_constraints

    # Non-area globals are trip-wide and must still reach every segment.
    assert "wifi" in beach.hard_constraints
    assert "wifi" in downtown.hard_constraints


def test_segment_without_its_own_area_still_inherits_global_areas():
    """Single-segment plans and un-distributed constraints are unaffected."""
    sq = StructuredQuery(city="Lisbon", hard_constraints=["near the centre", "balcony"])
    seg = itinerary._segment_query(sq, {"hard_constraints": []})
    assert "near the centre" in seg.hard_constraints
    assert "balcony" in seg.hard_constraints


def test_avoid_constraints_are_never_treated_as_segment_areas():
    """'avoid X' is trip-wide — it must reach a segment that has its own area."""
    sq = StructuredQuery(
        city="Amsterdam",
        hard_constraints=["avoid de pijp", "near the centre"],
    )
    seg = itinerary._segment_query(sq, {"hard_constraints": ["near the station"]})
    assert "avoid de pijp" in seg.hard_constraints
    assert "near the station" in seg.hard_constraints
    assert "near the centre" not in seg.hard_constraints


def test_area_detection_matches_retrievals_own_prefix_list():
    """Drift between these two silently breaks the scoping above."""
    for prefix in retrieval._NEAR_PREFIXES:
        assert itinerary._is_area_constraint(prefix + "the beach")
    assert not itinerary._is_area_constraint("balcony")
    assert not itinerary._is_area_constraint("avoid de pijp")


def test_empty_plan_is_not_reported_as_within_budget(monkeypatch):
    """An empty plan must never claim to be within budget.

    `total_cost` is 0.0 when nothing matched, so a naive
    `total_cost <= budget_total` returns True — telling every caller the
    opposite of the truth. The UI would render a green within-budget badge over
    zero stays, and WS3's planner replan cycle would never fire because the
    graph believed the plan succeeded. Found exactly that way: a $6 trip budget
    produced stays=0, total_cost=0.0, within_budget=True.
    """
    _force_single_segment(monkeypatch)
    _always_available(monkeypatch)
    _wire_retrieve(monkeypatch, default_cards=[])

    plan = _run(itinerary.plan_itinerary(_sq(budget_total=6.0)))

    assert plan["stays"] == []
    assert plan["within_budget"] is None, "empty plan must be 'unknown', not True"


def test_within_budget_still_computed_when_stays_exist(monkeypatch):
    """The guard above must not suppress the real budget check."""
    _force_single_segment(monkeypatch)
    _always_available(monkeypatch)
    _wire_retrieve(monkeypatch, default_cards=[_card("cheap", price=50, rating=4.0)])

    under = _run(itinerary.plan_itinerary(_sq(budget_total=10_000.0)))
    over = _run(itinerary.plan_itinerary(_sq(budget_total=1.0)))

    assert under["stays"] and under["within_budget"] is True
    assert over["stays"] and over["within_budget"] is False
