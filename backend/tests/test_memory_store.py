"""Unit tests for the memory store's pure logic (WS1).

Scope note: `validate_dealbreaker` and `extract_dealbreakers` are deliberately
pure — no mem0, no network, no LLM — which is the whole point of deciding
dealbreaker polarity at write time. That makes them cheap to test exhaustively
here, and it is the part that must never regress: these functions decide what
becomes a HARD Qdrant filter, so a bug silently empties a user's results.

`mem0` is stubbed rather than installed-and-mocked so this file runs in the
lint/test CI job, which installs only requirements-dev.txt.
"""
from __future__ import annotations

import sys
import types

import pytest


@pytest.fixture(scope="module")
def store():
    """Import app.memory.store with mem0 and the embedder stubbed out."""
    if "mem0" not in sys.modules:
        mem0 = types.ModuleType("mem0")

        class _Memory:
            @classmethod
            def from_config(cls, _config):  # pragma: no cover - never called here
                return cls()

        mem0.Memory = _Memory
        sys.modules["mem0"] = mem0

    # fastembed_embedder imports mem0.embeddings.base and app.embeddings; the
    # store only needs the names, so stub the base class.
    if "mem0.embeddings.base" not in sys.modules:
        base = types.ModuleType("mem0.embeddings.base")

        class EmbeddingBase:
            def __init__(self, config=None):
                self.config = config

        base.EmbeddingBase = EmbeddingBase
        sys.modules["mem0.embeddings"] = types.ModuleType("mem0.embeddings")
        sys.modules["mem0.embeddings.base"] = base

    from app.memory import store as _store

    return _store


# ── validate_dealbreaker ─────────────────────────────────────────────────────

def test_valid_amenity_dealbreaker_both_directions(store):
    assert store.validate_dealbreaker("amenities", "elevator", "must") == {
        "field": "amenities",
        "value": "elevator",
        "op": "must",
    }
    assert store.validate_dealbreaker("amenities", "elevator", "must_not") == {
        "field": "amenities",
        "value": "elevator",
        "op": "must_not",
    }


def test_pets_allowed_is_valid_in_both_directions(store):
    """The case that proves polarity cannot be derived from the field.

    `pets_allowed` means "pets are permitted", so a dog owner needs `must` and
    an allergy sufferer needs `must_not` on the SAME field. Any rule inferring
    direction from the field name is wrong for one of them.
    """
    assert store.validate_dealbreaker("amenities", "pets_allowed", "must")
    assert store.validate_dealbreaker("amenities", "pets_allowed", "must_not")


def test_valid_room_type_dealbreaker(store):
    assert store.validate_dealbreaker("type", "Shared room", "must_not") == {
        "field": "type",
        "value": "Shared room",
        "op": "must_not",
    }


def test_room_type_is_case_sensitive(store):
    """Values are matched verbatim against the Qdrant payload.

    Ingestion stores the real Inside Airbnb strings, so "shared room" would
    filter to zero results while looking perfectly reasonable in the panel.
    """
    assert store.validate_dealbreaker("type", "shared room", "must_not") is None


@pytest.mark.parametrize(
    "field,value,op",
    [
        ("amenities", "helipad", "must"),        # not in the 18-term vocabulary
        ("price", "100", "must"),                 # not a filterable field
        ("type", "Castle", "must_not"),           # not a real room type
        ("amenities", "elevator", "maybe"),       # not a valid direction
        ("amenities", "elevator", None),
        (None, None, None),
    ],
)
def test_unenforceable_dealbreakers_are_rejected(store, field, value, op):
    """Fails closed: the caller records these as soft preferences instead."""
    assert store.validate_dealbreaker(field, value, op) is None


def test_vocabulary_matches_the_shipped_payload(store):
    """Drift here produces filters that can never match anything."""
    assert len(store.CANONICAL_AMENITIES) == 18
    assert store.ROOM_TYPES == {
        "Entire home/apt",
        "Private room",
        "Shared room",
        "Hotel room",
    }


# ── extract_dealbreakers ─────────────────────────────────────────────────────

def _mem(text, **meta):
    return {"id": text, "text": text, "score": 0.9, "metadata": meta}


def test_partitions_by_direction(store):
    memories = {
        "traveller": [
            _mem("needs a lift", kind="dealbreaker", field="amenities",
                 value="elevator", op="must"),
            _mem("no shared rooms", kind="dealbreaker", field="type",
                 value="Shared room", op="must_not"),
        ],
        "trip": [],
    }
    out = store.extract_dealbreakers(memories)
    assert out["must"] == [{"field": "amenities", "value": "elevator"}]
    assert out["must_not"] == [{"field": "type", "value": "Shared room"}]
    assert out["unmapped"] == []


def test_non_dealbreaker_memories_are_ignored(store):
    """An ordinary preference must never silently become a hard filter."""
    memories = {"traveller": [_mem("likes rooftop bars")], "trip": []}
    out = store.extract_dealbreakers(memories)
    assert out == {"must": [], "must_not": [], "unmapped": []}


def test_unenforceable_dealbreaker_surfaces_as_unmapped_not_dropped(store):
    """The user believes this is enforced; the panel has to be able to say it isn't."""
    memories = {
        "traveller": [
            _mem("no shared bathrooms", kind="dealbreaker",
                 field="bathroom", value="shared", op="must_not"),
        ],
        "trip": [],
    }
    out = store.extract_dealbreakers(memories)
    assert out["must"] == []
    assert out["must_not"] == []
    assert out["unmapped"] == ["no shared bathrooms"]


def test_duplicate_conditions_are_deduped(store):
    memories = {
        "traveller": [
            _mem("a", kind="dealbreaker", field="amenities", value="elevator", op="must"),
            _mem("b", kind="dealbreaker", field="amenities", value="elevator", op="must"),
        ],
        "trip": [],
    }
    assert store.extract_dealbreakers(memories)["must"] == [
        {"field": "amenities", "value": "elevator"}
    ]


def test_no_polarity_guessing_from_text(store):
    """The failure mode that motivated write-time capture.

    "the elevator was broken, avoid this place" mentions a vocabulary term but
    means the opposite. Without metadata it must produce NO filter at all.
    """
    memories = {
        "traveller": [_mem("the elevator was broken, avoid this place")],
        "trip": [],
    }
    out = store.extract_dealbreakers(memories)
    assert out["must"] == []
    assert out["must_not"] == []


def test_empty_input_is_safe(store):
    assert store.extract_dealbreakers({}) == {
        "must": [], "must_not": [], "unmapped": []
    }


# ── as_prompt_context ────────────────────────────────────────────────────────

def test_prompt_context_is_empty_when_nothing_recalled(store):
    """An empty string means the intent prompt is byte-identical to pre-memory."""
    assert store.as_prompt_context({"traveller": [], "trip": []}) == ""


def test_prompt_context_marks_trip_scope_and_defers_to_current_request(store):
    out = store.as_prompt_context({
        "traveller": [_mem("hates stairs")],
        "trip": [_mem("going to Lisbon")],
    })
    assert "hates stairs" in out
    assert "(this trip) going to Lisbon" in out
    # Without this the model treats a remembered budget as binding and quietly
    # overrides an explicit "splurge tonight".
    assert "unless the current request contradicts" in out


# ── recall: rules by kind, preferences by similarity ─────────────────────────

async def test_recall_returns_dealbreakers_even_when_similarity_misses(store, monkeypatch):
    """Regression for the guarantee that standing rules always bind.

    Measured before the fix: with one dealbreaker plus ten unrelated memories,
    the rule fell outside the top-6 similarity window for 3 of 3 ordinary
    queries, so the hard filter silently stopped applying. Rules are now fetched
    by `kind`, never ranked against the query.
    """
    rule = {
        "id": "rule-1", "text": "Never show: type = Shared room", "score": 1.0,
        "metadata": {"kind": "dealbreaker", "field": "type",
                     "value": "Shared room", "op": "must_not"},
    }
    similar = [
        {"id": f"m{i}", "text": f"noise {i}", "score": 0.4, "metadata": {}}
        for i in range(6)
    ]
    monkeypatch.setattr(store, "_search_sync", lambda q, k, lim: list(similar))
    monkeypatch.setattr(store, "_dealbreakers_sync", lambda k: [dict(rule)])

    out = await store.recall("a place with a pool in Lisbon", user_id="u1")

    assert store.extract_dealbreakers(out)["must_not"] == [
        {"field": "type", "value": "Shared room"}
    ]
    # Rules are surfaced first so the panel shows the binding constraint on top.
    assert out["traveller"][0]["id"] == "rule-1"


async def test_recall_does_not_duplicate_a_rule_that_also_ranks(store, monkeypatch):
    rule = {
        "id": "rule-1", "text": "Never show: type = Shared room", "score": 1.0,
        "metadata": {"kind": "dealbreaker", "field": "type",
                     "value": "Shared room", "op": "must_not"},
    }
    monkeypatch.setattr(store, "_search_sync", lambda q, k, lim: [dict(rule)])
    monkeypatch.setattr(store, "_dealbreakers_sync", lambda k: [dict(rule)])

    out = await store.recall("shared rooms", user_id="u1")
    assert [m["id"] for m in out["traveller"]] == ["rule-1"]


async def test_recall_survives_a_dealbreaker_fetch_failure(store, monkeypatch):
    """Memory is an enhancement — a failed rule fetch must not break the turn."""
    def boom(_k):
        raise RuntimeError("qdrant down")

    monkeypatch.setattr(store, "_search_sync", lambda q, k, lim: [])
    monkeypatch.setattr(store, "_dealbreakers_sync", boom)

    out = await store.recall("anything", user_id="u1")
    assert out == {"traveller": [], "trip": []}
