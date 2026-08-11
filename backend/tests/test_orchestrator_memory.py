"""Tests for the WS1 memory hooks in app.agents.orchestrator.

These cover the wiring, not mem0 itself: that a turn WITHOUT a user_id is
byte-identical to the pre-memory pipeline, that recall/write step events are
emitted with the phases the frontend panel switches on, that validated
dealbreakers reach retrieval as HARD conditions, and that the override path
fires when those conditions empty the result set.

Everything is stubbed — zero network, zero LLM quota, mem0 never imported.
"""
from __future__ import annotations

import pytest

from app.agents import orchestrator
from app.schemas import ConciergeRequest, ListingCard


def _card(cid: str, name: str) -> ListingCard:
    return ListingCard(
        id=cid, name=name, type="Entire home/apt", city="Lisbon",
        neighbourhood="Alfama", lat=38.7, lng=-9.1, price_per_night=100.0,
    )


class _FakeStore:
    """Stand-in for app.memory.store with the same contract."""

    def __init__(self, traveller=None, dealbreakers=None):
        self._traveller = traveller or []
        self._dealbreakers = dealbreakers or {"must": [], "must_not": [], "unmapped": []}
        self.remember_calls: list[dict] = []
        self.revoked_calls: list[tuple] = []

    async def recall(self, query, user_id, trip_id=None):
        return {"traveller": self._traveller, "trip": []}

    def extract_dealbreakers(self, memories):
        return self._dealbreakers

    def as_prompt_context(self, memories):
        return "KNOWN TRAVELLER PREFERENCES:\n- hates stairs" if self._traveller else ""

    async def remember(self, q, a, user_id, trip_id=None, dealbreakers=None,
                       trip_state=None):
        self.remember_calls.append(
            {"query": q, "answer": a, "user_id": user_id, "trip_id": trip_id,
             "dealbreakers": dealbreakers, "trip_state": trip_state}
        )
        return [{"id": "m1", "text": "User hates stairs"}]

    async def revoke_dealbreakers(self, user_id, terms):
        self.revoked_calls.append((user_id, terms))
        return [f"revoked::{t}" for t in terms]


@pytest.fixture
def wired(monkeypatch):
    """Neutralise the LLM and retrieval so only the memory wiring is exercised."""
    async def fake_parse_intent(query, step=None, memory_context=None):
        fake_parse_intent.memory_context = memory_context
        from app.schemas import StructuredQuery
        return StructuredQuery(city="Lisbon")

    fake_parse_intent.memory_context = "UNSET"
    monkeypatch.setattr(orchestrator.intent, "parse_intent", fake_parse_intent)

    calls: list[dict] = []

    async def fake_retrieve(sq, limit=20, exclude=None):
        calls.append({"limit": limit, "exclude": exclude})
        return [(_card("l1", "Quiet Flat"), "rationale")]

    monkeypatch.setattr(orchestrator.retrieval, "retrieve", fake_retrieve)

    class _Stream:
        measured = False
        usage = None

        def __aiter__(self):
            async def gen():
                yield "Here you go."
            return gen()

    monkeypatch.setattr(
        orchestrator.llm, "stream_text_with_usage", lambda *a, **k: _Stream()
    )
    return {"intent": fake_parse_intent, "retrieve_calls": calls}


async def _drain(req):
    return [ev async for ev in orchestrator.run_concierge(req)]


# ── memory disabled / anonymous ──────────────────────────────────────────────

async def test_no_user_id_emits_no_memory_events(wired, monkeypatch):
    """An anonymous turn must behave exactly as it did before WS1."""
    monkeypatch.setattr(orchestrator, "_memory_store", lambda: _FakeStore())
    events = await _drain(ConciergeRequest(query="a flat in Lisbon"))
    assert not [e for e in events if e.get("agent") == "memory"]
    # …and retrieval must not be given an exclude set.
    assert all(c["exclude"] is None for c in wired["retrieve_calls"])


async def test_memory_store_unavailable_does_not_break_the_turn(wired, monkeypatch):
    """mem0 missing or Qdrant down must degrade, not raise."""
    monkeypatch.setattr(orchestrator, "_memory_store", lambda: None)
    events = await _drain(
        ConciergeRequest(query="a flat in Lisbon", user_id="u1")
    )
    assert not [e for e in events if e.get("agent") == "memory"]
    assert any(e["type"] == "done" for e in events)


# ── recall + write hooks ─────────────────────────────────────────────────────

async def test_recall_and_write_emit_phased_step_events(wired, monkeypatch):
    """The panel switches on data.phase; both phases arrive as agent 'memory'."""
    store = _FakeStore(traveller=[{"id": "m1", "text": "hates stairs", "score": 0.9}])
    monkeypatch.setattr(orchestrator, "_memory_store", lambda: store)

    events = await _drain(
        ConciergeRequest(query="a flat in Lisbon", user_id="u1", trip_id="t1")
    )
    mem = [e for e in events if e.get("agent") == "memory" and e["status"] == "done"]
    phases = [e["data"]["phase"] for e in mem]
    assert phases == ["recall", "write"]
    assert mem[0]["data"]["traveller"][0]["text"] == "hates stairs"
    assert mem[1]["data"]["written"]


async def test_recall_context_is_passed_into_the_intent_call(wired, monkeypatch):
    store = _FakeStore(traveller=[{"id": "m1", "text": "hates stairs", "score": 0.9}])
    monkeypatch.setattr(orchestrator, "_memory_store", lambda: store)
    await _drain(ConciergeRequest(query="a flat in Lisbon", user_id="u1"))
    assert "hates stairs" in wired["intent"].memory_context


async def test_write_receives_the_streamed_answer(wired, monkeypatch):
    """The write needs the assistant turn, accumulated during streaming."""
    store = _FakeStore()
    monkeypatch.setattr(orchestrator, "_memory_store", lambda: store)
    await _drain(ConciergeRequest(query="a flat in Lisbon", user_id="u1"))
    assert store.remember_calls
    call = store.remember_calls[0]
    assert call["answer"] == "Here you go."
    assert call["user_id"] == "u1"


async def test_dealbreakers_from_this_turn_are_persisted(wired, monkeypatch):
    """The link that makes the feature work.

    mem0's own extraction produces free text with no metadata, and
    extract_dealbreakers() matches on metadata — so a rule captured by the
    intent step has to be handed to remember() explicitly or it can never be
    projected back into a filter on any later turn.
    """
    from app.schemas import Dealbreaker, StructuredQuery

    async def parse_with_rule(query, step=None, memory_context=None):
        return StructuredQuery(
            city="Lisbon",
            dealbreakers=[
                Dealbreaker(field="type", value="Shared room", op="must_not")
            ],
        )

    monkeypatch.setattr(orchestrator.intent, "parse_intent", parse_with_rule)
    store = _FakeStore()
    monkeypatch.setattr(orchestrator, "_memory_store", lambda: store)

    await _drain(ConciergeRequest(query="never show me shared rooms", user_id="u1"))

    assert store.remember_calls[0]["dealbreakers"] == [
        {"field": "type", "value": "Shared room", "op": "must_not"}
    ]


# ── dealbreakers as hard filters ─────────────────────────────────────────────

async def test_validated_dealbreakers_reach_retrieval_as_conditions(wired, monkeypatch):
    """The core guarantee: a standing rule is a filter, not a prompt hint."""
    store = _FakeStore(dealbreakers={
        "must": [{"field": "amenities", "value": "elevator"}],
        "must_not": [{"field": "type", "value": "Shared room"}],
        "unmapped": [],
    })
    monkeypatch.setattr(orchestrator, "_memory_store", lambda: store)
    await _drain(ConciergeRequest(query="a flat in Lisbon", user_id="u1"))

    exclude = wired["retrieve_calls"][0]["exclude"]
    assert exclude["must"] == [{"field": "amenities", "value": "elevator"}]
    assert exclude["must_not"] == [{"field": "type", "value": "Shared room"}]


async def test_unmapped_only_does_not_become_a_filter(wired, monkeypatch):
    """Unenforceable rules must never be passed off as hard conditions."""
    store = _FakeStore(dealbreakers={
        "must": [], "must_not": [], "unmapped": ["no shared bathrooms"],
    })
    monkeypatch.setattr(orchestrator, "_memory_store", lambda: store)
    events = await _drain(ConciergeRequest(query="a flat in Lisbon", user_id="u1"))

    assert wired["retrieve_calls"][0]["exclude"] is None
    recall = next(
        e for e in events
        if e.get("agent") == "memory" and e["status"] == "done"
        and e["data"]["phase"] == "recall"
    )
    # …but it still has to be visible, badged as soft, in the panel.
    assert recall["data"]["unmapped"] == ["no shared bathrooms"]


# ── override path ────────────────────────────────────────────────────────────

async def test_override_retries_unfiltered_when_dealbreakers_empty_results(monkeypatch):
    """A saved rule that returns nothing is worse than no rule — relax and say so."""
    attempts: list[dict | None] = []

    async def picky_retrieve(sq, limit=20, exclude=None):
        attempts.append(exclude)
        if exclude:
            return []
        return [(_card("l1", "Fallback Flat"), "rationale")]

    monkeypatch.setattr(orchestrator.retrieval, "retrieve", picky_retrieve)
    candidates, relaxed = await orchestrator._retrieve_with_override(
        None, 10,
        {"must": [{"field": "amenities", "value": "elevator"}],
         "must_not": [], "unmapped": []},
    )
    assert relaxed is True
    assert len(candidates) == 1
    assert attempts[0] is not None and attempts[1] is None


async def test_no_override_when_constrained_search_succeeds(monkeypatch):
    async def ok_retrieve(sq, limit=20, exclude=None):
        return [(_card("l1", "Lift Flat"), "rationale")]

    monkeypatch.setattr(orchestrator.retrieval, "retrieve", ok_retrieve)
    _c, relaxed = await orchestrator._retrieve_with_override(
        None, 10, {"must": [{"field": "amenities", "value": "elevator"}],
                   "must_not": [], "unmapped": []},
    )
    assert relaxed is False


def test_dealbreaker_note_discloses_application_and_relaxation():
    db = {"must": [{"field": "amenities", "value": "elevator"}],
          "must_not": [{"field": "type", "value": "Shared room"}], "unmapped": []}
    applied = orchestrator._dealbreaker_note(db, relaxed=False)
    assert "saved rules" in applied and "amenities=elevator" in applied

    relaxed = orchestrator._dealbreaker_note(db, relaxed=True)
    assert "relaxed" in relaxed

    assert orchestrator._dealbreaker_note(None, False) == ""


def test_already_stored_dealbreakers_are_not_rewritten():
    """Recalled rules are fed back into the intent prompt, so the model
    re-reports them; without this filter every turn stores another copy."""
    from app.schemas import Dealbreaker

    known = {
        "must": [{"field": "amenities", "value": "elevator"}],
        "must_not": [{"field": "type", "value": "Shared room"}],
        "unmapped": [],
    }
    extracted = [
        Dealbreaker(field="type", value="Shared room", op="must_not"),   # already known
        Dealbreaker(field="amenities", value="elevator", op="must"),     # already known
        Dealbreaker(field="amenities", value="pets_allowed", op="must_not"),  # new
    ]
    assert orchestrator._new_dealbreakers(extracted, known) == [
        {"field": "amenities", "value": "pets_allowed", "op": "must_not"}
    ]


def test_same_value_opposite_direction_counts_as_new():
    """`pets_allowed` must/must_not are different rules on the same field."""
    from app.schemas import Dealbreaker

    known = {"must": [{"field": "amenities", "value": "pets_allowed"}],
             "must_not": [], "unmapped": []}
    extracted = [Dealbreaker(field="amenities", value="pets_allowed", op="must_not")]
    assert orchestrator._new_dealbreakers(extracted, known) == [
        {"field": "amenities", "value": "pets_allowed", "op": "must_not"}
    ]


def test_new_dealbreakers_handles_empty_inputs():
    assert orchestrator._new_dealbreakers([], orchestrator._NO_DEALBREAKERS) == []
    assert orchestrator._new_dealbreakers(None, orchestrator._NO_DEALBREAKERS) == []


# ── revocation + trip state ──────────────────────────────────────────────────

async def test_suppress_dealbreakers_revokes_stored_rules(wired, monkeypatch):
    """'Actually, shared rooms are fine now' must undo the rule.

    Before this, the panel's forget button was the only revocation path — a
    rule stated in one sentence could only be undone with a mouse.
    """
    from app.schemas import StructuredQuery

    async def parse_with_suppression(query, step=None, memory_context=None):
        return StructuredQuery(city="Lisbon", suppress_dealbreakers=["shared rooms"])

    monkeypatch.setattr(orchestrator.intent, "parse_intent", parse_with_suppression)
    store = _FakeStore()
    monkeypatch.setattr(orchestrator, "_memory_store", lambda: store)

    events = await _drain(
        ConciergeRequest(query="shared rooms are fine now", user_id="u1")
    )
    assert store.revoked_calls == [("u1", ["shared rooms"])]
    write = next(
        e for e in events
        if e.get("agent") == "memory" and e["status"] == "done"
        and e["data"]["phase"] == "write"
    )
    # Surfaced so a rule disappearing is visible, not silent.
    assert write["data"]["revoked"] == ["revoked::shared rooms"]


async def test_no_revocation_call_when_nothing_suppressed(wired, monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(orchestrator, "_memory_store", lambda: store)
    await _drain(ConciergeRequest(query="a flat in Lisbon", user_id="u1"))
    assert store.revoked_calls == []


async def test_trip_state_is_derived_not_mirrored(wired, monkeypatch):
    """Trip scope carries structured trip facts, never a copy of preferences."""
    from app.schemas import StructuredQuery

    async def parse_full(query, step=None, memory_context=None):
        return StructuredQuery(
            city="Amsterdam", party_size=2, budget_per_night=150.0,
            soft_preferences=["quiet"], vibe="romantic",
        )

    monkeypatch.setattr(orchestrator.intent, "parse_intent", parse_full)
    store = _FakeStore()
    monkeypatch.setattr(orchestrator, "_memory_store", lambda: store)

    await _drain(
        ConciergeRequest(query="Amsterdam for 2", user_id="u1", trip_id="t1")
    )
    state = store.remember_calls[0]["trip_state"]
    assert state == {"city": "Amsterdam", "party_size": 2, "budget_per_night": 150.0}
    # Preferences belong to the traveller scope — leaking them here is exactly
    # the duplication the mirror design produced.
    assert "vibe" not in state and "soft_preferences" not in state


async def test_no_trip_state_without_a_trip_id(wired, monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(orchestrator, "_memory_store", lambda: store)
    await _drain(ConciergeRequest(query="a flat in Lisbon", user_id="u1"))
    assert store.remember_calls[0]["trip_state"] is None


def test_trip_state_drops_unset_fields():
    from app.schemas import StructuredQuery
    assert orchestrator._trip_state(StructuredQuery()) == {}
    assert orchestrator._trip_state(StructuredQuery(city="Lisbon")) == {"city": "Lisbon"}
