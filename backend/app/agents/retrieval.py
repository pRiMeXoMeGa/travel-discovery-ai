"""Retrieval agent: semantic + filtered + geospatial search.

Combines a Qdrant vector search (semantic, bge-small 384-dim — the SAME model
the corpus was embedded with) with hard-constraint payload filters (price, type,
amenities, rating) and an optional geospatial signal, then hydrates full
listing rows from Postgres and returns a ranked candidate set with a short
per-result rationale (brief §2.3).

Grounding: rationales are built deterministically from real payload/row fields
(price, rating, amenities, neighbourhood) — NOT free-text LLM generation — so a
rationale can never claim an attribute the listing does not have.

Memory: candidate sets are bounded by `limit` (default 20, hard cap 50).
"""
import logging
from typing import Any

from qdrant_client import models as qmodels

from ..cache import cache_get, cache_set
from ..db import get_pool
from ..embeddings import embed_query
from ..schemas import ListingCard, StructuredQuery
from ..vectorstore import get_qdrant
from ..config import settings

logger = logging.getLogger(__name__)

_HARD_CAP = 50

# Amenity vocabulary used by the search router — drives constraint -> filter.
_KNOWN_AMENITIES = {
    "wifi", "pool", "hot_tub", "gym", "parking", "kitchen", "ac",
    "pets_allowed", "balcony", "breakfast_included", "workspace", "bbq",
    "beach_access", "concierge", "ev_charger", "elevator", "washer",
    "dryer", "heating", "tv",
}
_KEY_AMENITY_PRIORITY = [
    "wifi", "pool", "hot_tub", "gym", "parking", "kitchen", "ac",
    "pets_allowed", "balcony", "breakfast_included", "workspace", "bbq",
]
_TYPE_KEYWORDS = {
    "apartment": "entire place",
    "flat": "entire place",
    "entire place": "entire place",
    "private room": "private room",
    "room": "private room",
    "hotel": "hotel",
}


def _normalize_amenity(token: str) -> str | None:
    t = token.lower().strip().replace(" ", "_").replace("-", "_")
    if t in _KNOWN_AMENITIES:
        return t
    # a few synonyms
    synonyms = {
        "air_conditioning": "ac", "aircon": "ac", "swimming_pool": "pool",
        "pet_friendly": "pets_allowed", "pets": "pets_allowed",
        "wi_fi": "wifi", "internet": "wifi", "car_park": "parking",
        "beach": "beach_access",
    }
    return synonyms.get(t)


def _parse_constraints(sq: StructuredQuery) -> dict[str, Any]:
    """Translate the StructuredQuery into structured filter primitives.

    Returns dict with keys: amenities(list), avoid_areas(list[str]),
    near_areas(list[str]), property_type(str|None).
    """
    amenities: list[str] = []
    avoid_areas: list[str] = []
    near_areas: list[str] = []
    property_type: str | None = None

    for raw in [*sq.hard_constraints, *sq.soft_preferences]:
        low = raw.lower().strip()
        # area avoidance / inclusion
        if low.startswith("avoid ") or low.startswith("not "):
            avoid_areas.append(low.split(" ", 1)[1].strip())
            continue
        if low.startswith("near ") or low.startswith("close to "):
            near_areas.append(low.replace("close to", "").replace("near", "").strip())
        # property type
        for kw, canonical in _TYPE_KEYWORDS.items():
            if kw in low:
                property_type = canonical
        # amenity
        am = _normalize_amenity(low)
        if am:
            amenities.append(am)

    # dedupe preserving order
    amenities = list(dict.fromkeys(amenities))
    return {
        "amenities": amenities,
        "avoid_areas": list(dict.fromkeys(avoid_areas)),
        "near_areas": list(dict.fromkeys(near_areas)),
        "property_type": property_type,
    }


def _build_qdrant_filter(sq: StructuredQuery, parsed: dict) -> qmodels.Filter | None:
    """Build a Qdrant payload filter from hard constraints.

    Listings payload fields: listing_id, name, type, city, neighbourhood, lat,
    lng, base_price, beds, rating, amenities.
    """
    must: list[qmodels.Condition] = []
    must_not: list[qmodels.Condition] = []

    if sq.city:
        must.append(
            qmodels.FieldCondition(
                key="city", match=qmodels.MatchValue(value=sq.city)
            )
        )
    price_cap = sq.budget_per_night
    if price_cap is not None:
        must.append(
            qmodels.FieldCondition(key="base_price", range=qmodels.Range(lte=price_cap))
        )
    if sq.party_size and sq.party_size > 1:
        must.append(
            qmodels.FieldCondition(key="beds", range=qmodels.Range(gte=sq.party_size))
        )
    if parsed["property_type"]:
        must.append(
            qmodels.FieldCondition(
                key="type", match=qmodels.MatchValue(value=parsed["property_type"])
            )
        )
    for am in parsed["amenities"]:
        # amenities stored as a JSON array in the payload; MatchValue on an
        # array field matches when the value is a member.
        must.append(
            qmodels.FieldCondition(key="amenities", match=qmodels.MatchValue(value=am))
        )
    for area in parsed["avoid_areas"]:
        # case-insensitive-ish: payload neighbourhoods are title-cased.
        must_not.append(
            qmodels.FieldCondition(
                key="neighbourhood", match=qmodels.MatchValue(value=area.title())
            )
        )

    if not must and not must_not:
        return None
    return qmodels.Filter(must=must or None, must_not=must_not or None)


def _query_text(sq: StructuredQuery) -> str:
    """Compose the semantic query string from the structured intent."""
    parts: list[str] = []
    if sq.vibe:
        parts.append(sq.vibe)
    parts.extend(sq.soft_preferences)
    parts.extend(sq.hard_constraints)
    if sq.city:
        parts.append(f"in {sq.city}")
    if not parts:
        parts.append(sq.city or "place to stay")
    return " ".join(parts)


def _rationale(payload: dict, parsed: dict, score: float) -> str:
    """Deterministic, grounded rationale from real payload fields."""
    bits: list[str] = []
    matched = [a for a in parsed["amenities"] if a in (payload.get("amenities") or [])]
    if matched:
        bits.append("has " + ", ".join(matched))
    rating = payload.get("rating")
    if rating:
        bits.append(f"rated {round(float(rating), 2)}")
    nb = payload.get("neighbourhood")
    if nb:
        bits.append(f"in {nb}")
    price = payload.get("base_price")
    if price is not None:
        bits.append(f"${round(float(price))}/night")
    bits.append(f"semantic match {score:.2f}")
    return "; ".join(bits)


async def _hydrate(listing_ids: list[str]) -> dict[str, Any]:
    """Fetch full listing rows from Postgres keyed by id (single query)."""
    if not listing_ids:
        return {}
    placeholders = ", ".join(f"${i + 1}" for i in range(len(listing_ids)))
    sql = (
        "SELECT id, name, type, city, neighbourhood, lat, lng, base_price, beds, "
        "amenities, photos, rating, review_count "
        f"FROM listings WHERE id IN ({placeholders})"
    )
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *listing_ids)
    return {r["id"]: r for r in rows}


def _cache_key(sq: StructuredQuery, limit: int) -> str:
    import hashlib, json
    raw = json.dumps(sq.model_dump(mode="json"), sort_keys=True, default=str)
    return "retrieval:" + hashlib.sha256((raw + f"|{limit}").encode()).hexdigest()


async def retrieve(
    sq: StructuredQuery, limit: int = 20
) -> list[tuple[ListingCard, str]]:
    """Return ranked (ListingCard, rationale) candidates.

    Pipeline:
      1. embed the composed query intent (bge-small) -> Qdrant `listings` search.
      2. apply hard constraints as Qdrant payload filters (price/type/amenities/area).
      3. hydrate full rows from Postgres; build grounded per-result rationales.
    If the filtered semantic search yields nothing (over-constrained), retry once
    without amenity/area filters so we degrade to a looser but still city/price
    bounded result rather than returning empty.
    """
    limit = max(1, min(limit, _HARD_CAP))

    key = _cache_key(sq, limit)
    try:
        cached = await cache_get(key)
        if cached:
            return [(ListingCard(**c), r) for c, r in cached]
    except Exception as exc:  # noqa: BLE001 — cache is best-effort
        logger.warning("retrieval cache_get failed: %s", exc)

    parsed = _parse_constraints(sq)
    qtext = _query_text(sq)
    vector = await embed_query(qtext)
    client = get_qdrant()

    async def _search(qfilter):
        return await client.search(
            collection_name=settings.qdrant_collection_listings,
            query_vector=vector,
            query_filter=qfilter,
            limit=limit,
            with_payload=True,
        )

    qfilter = _build_qdrant_filter(sq, parsed)
    hits = await _search(qfilter)

    # Over-constrained fallback: relax amenity + area filters, keep city/price.
    if not hits and qfilter is not None:
        relaxed = StructuredQuery(
            city=sq.city,
            party_size=sq.party_size,
            budget_per_night=sq.budget_per_night,
        )
        loose = _build_qdrant_filter(relaxed, _parse_constraints(relaxed))
        hits = await _search(loose)

    ids = [h.payload.get("listing_id") for h in hits if h.payload]
    rows = await _hydrate([i for i in ids if i])

    results: list[tuple[ListingCard, str]] = []
    for h in hits:
        if not h.payload:
            continue
        lid = h.payload.get("listing_id")
        row = rows.get(lid)
        if row is None:
            continue
        amenities = list(row["amenities"]) if row["amenities"] else []
        photos = list(row["photos"]) if row["photos"] else []
        card = ListingCard(
            id=row["id"],
            name=row["name"],
            type=row["type"],
            city=row["city"],
            neighbourhood=row["neighbourhood"],
            lat=float(row["lat"]),
            lng=float(row["lng"]),
            price_per_night=float(row["base_price"]),
            rating=float(row["rating"]) if row["rating"] is not None else None,
            review_count=row["review_count"] or 0,
            key_amenities=[a for a in _KEY_AMENITY_PRIORITY if a in amenities][:4],
            photo=photos[0] if photos else None,
        )
        results.append((card, _rationale(h.payload, parsed, h.score)))

    try:
        await cache_set(
            key,
            [(c.model_dump(mode="json"), r) for c, r in results],
            ttl=300,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("retrieval cache_set failed: %s", exc)

    return results
