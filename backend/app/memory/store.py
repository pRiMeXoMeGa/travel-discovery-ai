"""Traveller + trip memory (mem0), wrapped to match this codebase's async style.

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
import re
from typing import Any, Optional

from ..config import settings

# MUST precede `import mem0`: mem0 initialises its PostHog client at module
# import time, so setting this inside init_memory() would be too late — the
# first outbound analytics call has already been made by then. Kept as an
# explicit env write rather than a config kwarg because that is the only knob
# mem0 reads for this.
# getattr, not attribute access: this runs at IMPORT time, and settings is
# swapped for a lightweight stand-in in parts of the test suite. Defaulting to
# False also fails safe — an unexpected settings shape disables telemetry rather
# than silently enabling it.
if not getattr(settings, "mem0_telemetry", False):
    os.environ.setdefault("MEM0_TELEMETRY", "False")

from mem0 import Memory  # noqa: E402 — must import after the telemetry guard

from .fastembed_embedder import FastEmbedEmbedder, assert_local_and_gemini  # noqa: E402

logger = logging.getLogger(__name__)

TRAVELLER_LIMIT = 6
TRIP_LIMIT = 6

# Measured against gemini-3.1-flash-lite with the models baked into the image:
# a real inferred add() takes 3.8-7.9s (extraction + mem0's update-decision call
# + the Qdrant round-trip). The original 8.0 guess sat directly on that range, so
# writes timed out on the slower half of runs and the panel showed nothing.
#
# NOTE this bounds how long we WAIT, not the write itself: on timeout the worker
# thread carries on and mem0 still stores the memory. The user just doesn't see
# it until the next turn recalls it.
WRITE_TIMEOUT_S = 15.0

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
                "collection_name": settings.qdrant_collection_memories,
                # Must match listings/summaries — see config.py.
                "embedding_model_dims": settings.embedding_dim,
            },
        },
        "llm": {
            "provider": "gemini",
            "config": {
                "model": settings.gemini_model,
                "temperature": 0.0,
                # Passed explicitly rather than left to the environment.
                # mem0 2.x's Gemini provider falls back to `GOOGLE_API_KEY`
                # (NOT `GEMINI_API_KEY`, which is what this app and render.yaml
                # set), so relying on the env would fail with an opaque
                # "Missing key inputs argument" from google-genai at startup.
                "api_key": settings.gemini_api_key,
            },
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

    # The key is passed directly in _build_config (see the note there), which is
    # what actually makes Gemini work. GOOGLE_API_KEY is exported as well only
    # so any *other* google-genai code path inside mem0 finds a key instead of
    # raising: this app loads config via pydantic-settings, which parses .env
    # WITHOUT exporting to the environment, so under a bare `uvicorn` nothing
    # would be set at all.
    if settings.gemini_api_key and not os.environ.get("GOOGLE_API_KEY"):
        os.environ["GOOGLE_API_KEY"] = settings.gemini_api_key

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
    """Search one scope.

    mem0 2.x changed this signature: scoping moved from a top-level `user_id`
    kwarg into `filters`, and `limit` became `top_k`. Both matter more than they
    look — `search()` RAISES ValueError when `filters` carries none of
    user_id/agent_id/run_id, and `recall()` swallows exceptions by design, so
    the pre-2.x call shape would have made memory silently return empty forever
    while appearing correctly wired. Pinned to `mem0ai==2.0.*` for this reason.
    """
    return _normalise(
        init_memory().search(
            query=query, filters={"user_id": scope_key}, top_k=limit
        )
    )


# Standing rules are few by nature (a traveller has a handful, not hundreds), so
# one unfiltered page is enough and keeps this a single round-trip.
DEALBREAKER_LIMIT = 50


def _dealbreakers_sync(scope_key: str) -> list[dict]:
    """Fetch every stored dealbreaker for a scope, by kind — NOT by similarity.

    This is deliberately not part of the `search()` above. `search()` ranks by
    cosine similarity against the current query and returns the top 6, which is
    right for preferences ("show me what's relevant to this question") and
    completely wrong for standing rules.

    Measured, not assumed: with a dealbreaker plus ten unrelated memories,
    "Never show: type = Shared room" failed to appear in the top 6 for 3 of 3
    ordinary queries — so the hard filter silently stopped applying. A guarantee
    the traveller set months ago cannot be gated on an embedding coin flip; it
    either always binds or it isn't a guarantee.
    """
    try:
        raw = init_memory().get_all(
            filters={"user_id": scope_key, "kind": "dealbreaker"},
            top_k=DEALBREAKER_LIMIT,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("dealbreaker fetch failed: %s", exc)
        return []
    return _normalise(raw)


def _trip_state_sync(scope_key: str) -> list[dict]:
    """Fetch this trip's derived state row. By kind, never by similarity."""
    try:
        raw = init_memory().get_all(
            filters={"user_id": scope_key, "kind": "trip_state"},
            top_k=DEALBREAKER_LIMIT,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("trip-state fetch failed: %s", exc)
        return []
    return _normalise(raw)


async def recall(
    query: str, user_id: str, trip_id: Optional[str] = None
) -> dict[str, list[dict]]:
    """Fetch traveller + trip memories in parallel. Never raises.

    Returns {"traveller": [...], "trip": [...]}, each item carrying a score so
    the UI can show *why* a memory fired.
    """
    # Two different retrieval modes on purpose (see _dealbreakers_sync):
    # preferences by similarity, standing rules by kind. Both are local-embed +
    # Qdrant, so this stays at zero LLM calls.
    tasks = [
        asyncio.to_thread(_search_sync, query, user_id, TRAVELLER_LIMIT),
        asyncio.to_thread(_dealbreakers_sync, user_id),
    ]
    if trip_id:
        # Trip scope holds ONE derived-state row, so fetch it by kind too.
        # Ranking a single known row against the query would be pointless at
        # best and would drop it at worst.
        tasks.append(
            asyncio.to_thread(_trip_state_sync, f"trip::{trip_id}")
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

    similar, rules, trip = _ok(0), _ok(1), _ok(2)

    # Rules first, and de-duplicated: a dealbreaker that also happened to rank in
    # the similarity search must appear once, not twice.
    seen = {r["id"] for r in rules if r.get("id")}
    traveller = rules + [m for m in similar if m.get("id") not in seen]

    return {"traveller": traveller, "trip": trip}


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


def _dealbreaker_text(condition: dict) -> str:
    """Human-readable label for the memory panel.

    Stored as the memory body because that is what `recall()` returns and what
    the panel renders. The machine-readable triple lives in metadata — the text
    is never parsed back (see extract_dealbreakers).
    """
    verb = "Always require" if condition["op"] == "must" else "Never show"
    return f"{verb}: {condition['field']} = {condition['value']}"


def _write_dealbreakers_sync(
    conditions: list[dict], scope_key: str, trip_id: Optional[str]
) -> list[dict]:
    """Persist validated dealbreakers as tagged memories, one per rule.

    `infer=False` on purpose — these are already structured, so there is nothing
    for an LLM to extract, and re-extracting would lose the `op` polarity that
    the intent step captured while it could still see the sentence.

    Written separately from the inferred turn summary because mem0's extraction
    produces free text with no metadata, and `extract_dealbreakers()` matches on
    `metadata["kind"] == "dealbreaker"`. Without this write the whole chain is
    inert: intent extracts rules, the projection looks for them, and nothing
    ever connects the two.
    """
    out: list[dict] = []
    mem = init_memory()
    for condition in conditions:
        meta = {
            "kind": "dealbreaker",
            "field": condition["field"],
            "value": condition["value"],
            "op": condition["op"],
            "scope": "traveller",
        }
        if trip_id:
            meta["trip_id"] = trip_id
        text = _dealbreaker_text(condition)
        try:
            written = _normalise(
                mem.add(
                    [{"role": "user", "content": text}],
                    user_id=scope_key,
                    metadata=meta,
                    infer=False,
                )
            )
        except TypeError as exc:
            logger.warning("mem0 rejected infer=False for dealbreakers: %s", exc)
            return out
        # infer=False returns the stored rows; if a version returns nothing,
        # still surface the rule so the panel can show what was captured.
        out.extend(written or [{"id": "", "text": text, "score": 1.0, "metadata": meta}])
    return out


# Fields of the parsed StructuredQuery that describe THIS TRIP rather than the
# traveller. Deliberately excludes preferences/vibe — those belong to the
# traveller scope and would reintroduce the duplication described below.
TRIP_STATE_FIELDS = (
    "city", "check_in", "check_out", "party_size",
    "budget_per_night", "budget_total",
)


def _format_trip_state(state: dict) -> str:
    """One compact human-readable line for the panel and the prompt."""
    bits = []
    if state.get("city"):
        bits.append(str(state["city"]))
    if state.get("check_in") and state.get("check_out"):
        bits.append(f"{state['check_in']} to {state['check_out']}")
    if state.get("party_size"):
        bits.append(f"{state['party_size']} guest(s)")
    if state.get("budget_per_night"):
        bits.append(f"<= {state['budget_per_night']}/night")
    if state.get("budget_total"):
        bits.append(f"<= {state['budget_total']} total")
    return "Trip: " + ", ".join(bits) if bits else ""


def _write_trip_state_sync(trip_id: str, state: dict) -> list[dict]:
    """Upsert this trip's derived state as a single row. Zero LLM calls.

    UPSERT, not append: without deleting the previous row this would store one
    trip-state memory per turn and the scope would grow monotonically for the
    life of the trip.

    `infer=False` because the content is already structured — there is nothing
    for an LLM to extract, and letting mem0 re-extract would turn precise fields
    back into prose.
    """
    text = _format_trip_state(state)
    if not text:
        return []

    scope_key = f"trip::{trip_id}"
    mem = init_memory()

    # Drop the prior row(s) first. Fetched by kind so this never depends on
    # similarity ranking (same reasoning as _dealbreakers_sync).
    try:
        existing = _normalise(
            mem.get_all(
                filters={"user_id": scope_key, "kind": "trip_state"},
                top_k=DEALBREAKER_LIMIT,
            )
        )
        for row in existing:
            if row.get("id"):
                mem.delete(memory_id=row["id"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("trip-state cleanup failed (will append): %s", exc)

    meta = {"kind": "trip_state", "trip_id": trip_id, "scope": "trip", **state}
    try:
        written = _normalise(
            mem.add(
                [{"role": "user", "content": text}],
                user_id=scope_key,
                metadata=meta,
                infer=False,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("trip-state write failed: %s", exc)
        return []

    out = written or [{"id": "", "text": text, "score": 1.0, "metadata": meta}]
    for item in out:
        item["scope"] = "trip"
    return out


async def remember(
    user_query: str,
    assistant_answer: str,
    user_id: str,
    trip_id: Optional[str] = None,
    dealbreakers: Optional[list[dict]] = None,
    trip_state: Optional[dict] = None,
) -> list[dict]:
    """Extract and store memories from a completed turn. Never raises.

    The two scopes hold DIFFERENT KINDS of content, which is what makes them
    unable to diverge:

      * traveller (`user_id`) — preferences, from one inferred add()
        (1-2 Gemini calls), plus any validated dealbreakers written as tagged
        rules with infer=False (0 calls);
      * trip (`trip::<id>`)   — derived structured state from the parsed
        StructuredQuery (city/dates/party/budget), upserted with infer=False
        (0 calls).

    An earlier design mirrored the traveller facts into the trip scope instead.
    It was dropped after seeing the result: recall returned each fact twice and
    `as_prompt_context` rendered every line followed by an identical
    "(this trip) ..." line, doubling a memory block that is capped at 2000 chars
    and hardened against injection — for zero information gain. If both scopes
    hold the same facts there is no reason for two scopes.

    Preferences are still learned mid-trip, because the inferred write goes to
    the TRAVELLER scope; that is what makes "I hate stairs", said during a trip,
    survive into the next session.

    Costs LLM calls, so the orchestrator runs this AFTER the answer has finished
    streaming — the user already has their result before this begins.
    """
    messages = [
        {"role": "user", "content": user_query},
        {"role": "assistant", "content": assistant_answer},
    ]

    # Validate here, not at the call site: this is the boundary where an
    # unenforceable rule must be demoted rather than stored as if it were a
    # guarantee. Anything that fails validation is simply not written as a
    # dealbreaker — the inferred add() below still captures the sentence as an
    # ordinary preference, so nothing is lost, it just isn't claimed as binding.
    valid_conditions: list[dict] = []
    for raw in dealbreakers or []:
        condition = validate_dealbreaker(
            raw.get("field"), raw.get("value"), raw.get("op")
        )
        if condition is not None:
            valid_conditions.append(condition)
        else:
            logger.info("dropping unenforceable dealbreaker: %r", raw)

    async def _write_both() -> list[dict]:
        traveller = await asyncio.to_thread(
            _add_sync, messages, user_id, {"scope": "traveller"}, True
        )
        for item in traveller:
            item["scope"] = "traveller"

        if valid_conditions:
            rules = await asyncio.to_thread(
                _write_dealbreakers_sync, valid_conditions, user_id, trip_id
            )
            for item in rules:
                item["scope"] = "traveller"
            traveller = rules + traveller

        if not trip_id:
            return traveller

        trip = await asyncio.to_thread(_write_trip_state_sync, trip_id, trip_state)
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
    """Render memories for injection into the intent agent's prompt.

    Trip state is one compact line, not a per-fact repeat of the traveller
    block — see `remember()` for why the mirroring design was dropped.
    """
    lines = [f"- {item['text']}" for item in memories.get("traveller", [])]
    for item in memories.get("trip", []):
        lines.append(f"- (this trip) {item['text']}")
    if not lines:
        return ""
    return (
        "KNOWN TRAVELLER PREFERENCES (from previous sessions — apply unless the "
        "current request contradicts them):\n" + "\n".join(lines)
    )


async def revoke_dealbreakers(user_id: str, terms: list[str]) -> list[str]:
    """Delete stored dealbreakers the traveller just revoked in conversation.

    Backs `suppress_dealbreakers` — "actually, shared rooms are fine now".
    Without this the only revocation path is the panel's forget button, so a
    rule stated in one sentence could only be undone with a mouse.

    Matching is deliberately conservative. `terms` is free text from the intent
    model, so it is matched against the stored rule's `field`/`value` metadata
    and its rendered label, case-insensitively, on a word-boundary basis — never
    a loose substring. Deleting a standing rule the traveller did NOT ask to
    remove is the worse error: it silently un-enforces a guarantee, which is
    exactly the failure this feature exists to prevent. An unmatched term is
    left alone and reported back.

    Returns the ids actually deleted.
    """
    if not terms:
        return []

    def _revoke() -> list[str]:
        rules = _dealbreakers_sync(user_id)
        if not rules:
            return []
        needles = [t.strip().lower() for t in terms if t and t.strip()]
        deleted: list[str] = []
        mem = init_memory()
        for rule in rules:
            meta = rule.get("metadata") or {}
            value = str(meta.get("value", "")).lower()
            field = str(meta.get("field", "")).lower()
            haystack = f"{field} {value} {rule.get('text', '')}".lower()
            hit = any(
                # Whole-word containment in either direction: "shared rooms"
                # should revoke "Shared room", but "room" alone must not revoke
                # every room-typed rule.
                (value and value in needle)
                or (needle and re.search(rf"\b{re.escape(needle)}\b", haystack))
                for needle in needles
            )
            if hit and rule.get("id"):
                try:
                    mem.delete(memory_id=rule["id"])
                    deleted.append(rule["id"])
                except Exception as exc:  # noqa: BLE001
                    logger.warning("revoke failed for %s: %s", rule["id"], exc)
        return deleted

    try:
        return await asyncio.to_thread(_revoke)
    except Exception as exc:  # noqa: BLE001
        logger.warning("dealbreaker revocation failed: %s", exc)
        return []


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
