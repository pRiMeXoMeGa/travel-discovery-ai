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
from app.agents import itinerary  # noqa: E402
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
