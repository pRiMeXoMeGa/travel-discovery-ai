"""Traveller + trip memory (mem0), wrapped to match this codebase's async style.

DESTINATION: backend/app/memory/store.py
This file is staged in version2/ and will NOT import from there — the relative
imports below (..config, ..embeddings) only resolve once it sits inside the
`app` package. Move it, add backend/app/memory/__init__.py, and add `mem0` to
backend/requirements.txt before wiring anything up.

Every mem0 entry point is synchronous and CPU/network-bound, so each is pushed
off the event loop with asyncio.to_thread — the same pattern embeddings.py uses
for the model. Calling mem0 directly from run_concierge would block every other
SSE stream on the instance.

Cost model:
  search() -> local embed + Qdrant query. ZERO LLM calls.
  add()    -> ~1-2 Gemini calls (extraction + update decision) ONCE per turn,
              regardless of how many scopes are written. The trip scope is
              mirrored from the already-extracted facts with infer=False, which
              costs nothing and — more importantly — guarantees both scopes hold
              identical facts. Extracting the same turn twice is not just
              expensive, it is non-deterministic: the two runs can disagree and
              the scopes silently diverge. Never on the critical path; see
              remember() and the orchestrator hook.

Degradation: every function returns an empty/None result on failure rather than
raising. Memory is an enhancement — a mem0 outage must not break a search. This
mirrors how Redis failures are handled in v1: note that cache.py itself raises,
and the *callers* wrap it (see routers/search.py::_cache_get_safe and the inline
try/except in agents/retrieval.py). Here the swallowing lives in this module so
callers stay clean.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

from mem0 import Memory

from ..config import settings
from .fastembed_embedder import FastEmbedEmbedder, assert_local_and_gemini

logger = logging.getLogger(__name__)

TRAVELLER_LIMIT = 6
TRIP_LIMIT = 6
WRITE_TIMEOUT_S = 8.0

# Canonical amenity vocabulary — must stay identical to the 18 terms produced by
# ingestion/enrich.py::CANONICAL_AMENITIES, because these become Qdrant payload
# filters against the `amenities` field written in ingestion/ingest.py.
CANONICAL_AMENITIES = frozenset({
    "wifi", "pool", "kitchen", "parking", "balcony", "ac", "gym", "washer",
    "pets_allowed", "hot_tub", "bbq", "workspace", "beach_access", "concierge",
    "breakfast_included", "ev_charger", "elevator", "baby_cot",
})

# The four real Inside Airbnb room_type values stored verbatim in listings.type.
ROOM_TYPES = frozenset({
    "Entire home/apt", "Private room", "Shared room", "Hotel room",
})

# A dealbreaker is only enforceable if it names a real payload field, a value in
# that field's closed vocabulary, and an explicit direction. Polarity is NOT
# inferable from the field: `pets_allowed` means "pets are permitted", so an
# allergy sufferer needs must_not on the same field a dog owner needs must on.
# The extraction step decides direction while it can still see the sentence;
# this module only validates and projects.
DEALBREAKER_VOCAB: dict[str, frozenset] = {
    "amenities": CANONICAL_AMENITIES,
    "type": ROOM_TYPES,
}
DEALBREAKER_OPS = frozenset({"must", "must_not"})

_memory: Optional[Memory] = None


def _build_config() -> dict:
    return {
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "url": settings.qdrant_url,
                "api_key": settings.qdrant_api_key,
                "collection_name": "memories",
                "embedding_model_dims": 384,  # must match listings/summaries
            },
        },
        "llm": {
            "provider": "gemini",
            "config": {"model": settings.gemini_model, "temperature": 0.0},
        },
        "embedder": {
            "provider": "fastembed",
            "config": {"embedding_dims": 384},
        },
    }


def init_memory() -> Memory:
    """Construct mem0 once. Call from the FastAPI lifespan."""
    global _memory
    if _memory is not None:
        return _memory

    # mem0's Gemini provider reads GEMINI_API_KEY from os.environ directly, but
    # this app loads config through pydantic-settings, which parses .env WITHOUT
    # exporting to the environment. Under docker-compose (env_file: .env) real
    # env vars exist and this is a no-op; under a bare local `uvicorn` the key
    # would be missing and mem0 would silently fall back to another provider.
    if settings.gemini_api_key and not os.environ.get("GEMINI_API_KEY"):
        os.environ["GEMINI_API_KEY"] = settings.gemini_api_key

    try:
        from mem0.utils.factory import EmbedderFactory

        EmbedderFactory.provider_to_class["fastembed"] = (
            "app.memory.fastembed_embedder.FastEmbedEmbedder"
        )
    except (ImportError, AttributeError):
        logger.warning("mem0 EmbedderFactory hook unavailable; assigning directly")

    mem = Memory.from_config(_build_config())

    if not isinstance(getattr(mem, "embedding_model", None), FastEmbedEmbedder):
        mem.embedding_model = FastEmbedEmbedder(None)

    assert_local_and_gemini(mem)
    _memory = mem
    return _memory


def _normalise(raw: Any) -> list[dict]:
    """mem0 returns {"results": [...]} on v1.1 and a bare list on older versions."""
    if raw is None:
        return []
    items = raw.get("results", []) if isinstance(raw, dict) else raw
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "id": item.get("id"),
                "text": item.get("memory") or item.get("text", ""),
                "score": round(float(item.get("score") or 0.0), 4),
                "metadata": item.get("metadata") or {},
            }
        )
    return out


def _search_sync(query: str, scope_key: str, limit: int) -> list[dict]:
    return _normalise(
        init_memory().search(query=query, user_id=scope_key, limit=limit)
    )


async def recall(
    query: str, user_id: str, trip_id: Optional[str] = None
) -> dict[str, list[dict]]:
    """Fetch traveller + trip memories in parallel. Never raises.

    Returns {"traveller": [...], "trip": [...]}, each item carrying a score so
    the UI can show *why* a memory fired.
    """
    tasks = [asyncio.to_thread(_search_sync, query, user_id, TRAVELLER_LIMIT)]
    if trip_id:
        tasks.append(
            asyncio.to_thread(_search_sync, query, f"trip::{trip_id}", TRIP_LIMIT)
        )

    results = await asyncio.gather(*tasks, return_exceptions=True)

    def _ok(idx: int) -> list[dict]:
        if idx >= len(results):
            return []
        value = results[idx]
        if isinstance(value, Exception):
            logger.warning("memory recall failed: %s", value)
            return []
        return value

    return {"traveller": _ok(0), "trip": _ok(1)}


def _add_sync(
    messages: list[dict], scope_key: str, metadata: dict, infer: bool = True
) -> list[dict]:
    """Write to one mem0 scope.

    infer=True  -> mem0 runs LLM extraction + the update decision (1-2 calls).
    infer=False -> mem0 stores the supplied messages verbatim (0 calls). Used to
                   mirror already-extracted facts into a second scope.

    VERIFY on the mem0 version you pin: `infer` is accepted by Memory.add in
    current releases, but mem0's signature has moved before. If it raises
    TypeError the mirror is skipped (see remember) rather than silently falling
    back to a second extraction, which would let the two scopes diverge.
    """
    return _normalise(
        init_memory().add(messages, user_id=scope_key, metadata=metadata, infer=infer)
    )


async def remember(
    user_query: str,
    assistant_answer: str,
    user_id: str,
    trip_id: Optional[str] = None,
) -> list[dict]:
    """Extract and store memories from a completed turn. Never raises.

    Writes to BOTH scopes when a trip is active, from a SINGLE extraction:

      1. one inferred add() -> traveller scope   (1-2 Gemini calls)
      2. mirror the extracted facts -> trip scope with infer=False (0 calls)

    Writing only to the trip scope — as the first draft did — would mean no
    traveller-level preference is ever learned during a trip session, and "I
    hate stairs", said mid-trip, has to survive into the next session for the
    cross-session behaviour to exist at all.

    Extracting twice would cost 2-4 calls AND be non-deterministic: two runs over
    the same turn can extract different facts, so the scopes would silently
    diverge. Mirroring keeps them provably identical at 1-2 calls total, holding
    the "<= 4 Gemini calls per turn" ceiling regardless of trip state.

    Costs LLM calls, so the orchestrator runs this AFTER the answer has finished
    streaming — the user already has their result before this begins.
    """
    messages = [
        {"role": "user", "content": user_query},
        {"role": "assistant", "content": assistant_answer},
    ]

    async def _write_both() -> list[dict]:
        traveller = await asyncio.to_thread(
            _add_sync, messages, user_id, {"scope": "traveller"}, True
        )
        for item in traveller:
            item["scope"] = "traveller"

        if not trip_id or not traveller:
            return traveller

        # Mirror the SAME extracted facts into the trip scope, no re-extraction.
        mirror = [
            {"role": "user", "content": item["text"]}
            for item in traveller
            if item.get("text")
        ]
        if not mirror:
            return traveller

        try:
            trip = await asyncio.to_thread(
                _add_sync,
                mirror,
                f"trip::{trip_id}",
                {"scope": "trip", "trip_id": trip_id},
                False,
            )
        except TypeError as exc:
            # infer= not supported by this mem0 version. Skip the mirror rather
            # than re-extracting, which would let the scopes disagree.
            logger.warning(
                "mem0 does not accept infer=; trip scope not mirrored (%s)", exc
            )
            return traveller

        for item in trip:
            item["scope"] = "trip"
        return traveller + trip

    try:
        return await asyncio.wait_for(_write_both(), timeout=WRITE_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning("memory write exceeded %ss; abandoned", WRITE_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory write failed: %s", exc)
    return []


async def forget(memory_id: str) -> bool:
    """Delete one memory. Backs the forget button in the memory panel."""
    try:
        await asyncio.to_thread(init_memory().delete, memory_id=memory_id)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory delete failed: %s", exc)
        return False


# ── Prompt + filter projection ────────────────────────────────────────────────

def as_prompt_context(memories: dict[str, list[dict]]) -> str:
    """Render memories for injection into the intent agent's prompt."""
    lines = []
    for item in memories.get("traveller", []):
        lines.append(f"- {item['text']}")
    for item in memories.get("trip", []):
        lines.append(f"- (this trip) {item['text']}")
    if not lines:
        return ""
    return (
        "KNOWN TRAVELLER PREFERENCES (from previous sessions — apply unless the "
        "current request contradicts them):\n" + "\n".join(lines)
    )


def validate_dealbreaker(field: Any, value: Any, op: Any) -> Optional[dict]:
    """Return a validated {field, value, op} triple, or None if unenforceable.

    Called at WRITE time, by whatever turns an extracted dealbreaker into
    metadata (the extraction prompt's post-processing, and seed_memory.py).
    Validating here means an unenforceable dealbreaker is recorded as a soft
    preference from the start, instead of being silently dropped at read time —
    a dealbreaker the user believes is enforced but is not is worse than not
    having the feature at all.
    """
    if field not in DEALBREAKER_VOCAB or op not in DEALBREAKER_OPS:
        return None
    if value not in DEALBREAKER_VOCAB[field]:
        return None
    return {"field": field, "value": value, "op": op}


def extract_dealbreakers(memories: dict[str, list[dict]]) -> dict[str, list]:
    """Project dealbreaker memories into HARD Qdrant payload conditions.

    A dealbreaker expressed as a prompt hint is a suggestion the LLM may ignore.
    Expressed as a payload filter it is a guarantee — which is the whole point of
    the user having said "never show me this again".

    PURE PROJECTION. No keyword scanning, no polarity guessing, no LLM. Direction
    was decided at write time by the model that could still see the sentence, and
    validated then. Deriving it here would be wrong in both directions:

      * polarity is not a property of the field — `pets_allowed` means "pets are
        permitted", so an allergy sufferer needs must_not on the same field a dog
        owner needs must on;
      * scanning the memory text for a vocabulary term misfires on sentences like
        "the elevator was broken, avoid this place", which would map to
        *require elevator*.

    Returns conditions grouped by direction, ready for _build_qdrant_filter:

        {"must":     [{"field": "amenities", "value": "elevator"}],
         "must_not": [{"field": "type", "value": "Shared room"}],
         "unmapped": ["no shared bathrooms"]}

    `unmapped` is returned rather than dropped so the caller can pass it to the
    prompt as a soft preference AND the memory panel can badge it honestly.

    Requires the extraction prompt / seed_memory.py to tag dealbreaker memories
    with metadata {kind: "dealbreaker", field, value, op}, run through
    validate_dealbreaker() first.
    """
    must: list[dict] = []
    must_not: list[dict] = []
    unmapped: list[str] = []

    for item in memories.get("traveller", []):
        meta = item.get("metadata") or {}
        if meta.get("kind") != "dealbreaker":
            continue

        condition = validate_dealbreaker(
            meta.get("field"), meta.get("value"), meta.get("op")
        )
        if condition is None:
            # Recorded as a dealbreaker but not enforceable — surface it as soft
            # rather than pretending it filtered anything.
            text = item.get("text", "")
            if text:
                unmapped.append(text)
            continue

        target = must if condition["op"] == "must" else must_not
        target.append({"field": condition["field"], "value": condition["value"]})

    def _dedupe(rows: list[dict]) -> list[dict]:
        seen, out = set(), []
        for row in rows:
            key = (row["field"], row["value"])
            if key not in seen:
                seen.add(key)
                out.append(row)
        return out

    return {
        "must": _dedupe(must),
        "must_not": _dedupe(must_not),
        "unmapped": list(dict.fromkeys(unmapped)),
    }
