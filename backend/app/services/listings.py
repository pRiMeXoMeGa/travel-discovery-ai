"""Listing services: search/filter, listing detail, and compare.

Shared service layer used by `routers/search.py`, `routers/listings.py`,
`routers/agents.py`'s NL-search endpoint, `agents/orchestrator.py`'s
graceful-degradation fallback, and (WS2) MCP tools — see the package
docstring in `services/__init__.py` for the layering defect this fixes.

Behaviour-preserving refactor: every function below is the same SQL /
caching / availability logic that used to live directly in
`routers/search.py` and `routers/listings.py`. Request/response shapes on
the HTTP endpoints are unchanged.

Design notes (moved here from routers/search.py / routers/listings.py)
------------------------------------------------------------------------
* SQL is built by accumulating parameterized clauses into a list — user
  input NEVER reaches the query string (only via $N placeholders with
  asyncpg).
* Pagination is entirely at the DB level (LIMIT / OFFSET); we never load
  all rows into Python memory.
* Availability filter: when check_in/check_out are supplied, we first let
  SQL do all the cheap filters and then apply is_available_range() in
  Python on the returned page. This means the availability filter applies
  AFTER DB pagination, so `total` reflects pre-availability counts. This is
  an accepted simplification at the current scale (~1K listings); a future
  materialised-calendar table would fix it.
* Distance: when near_lat/near_lng are provided the haversine formula is
  computed inline in SQL (works fine for a few thousand rows; no PostGIS
  needed).
* Beds-as-capacity simplification: `guests` isn't a column; we use `beds`
  as a proxy for maximum capacity.
* Redis caching: search results are keyed by sha256 of the canonical JSON
  of SearchFilters (120s TTL); listing detail is keyed by `listing:{id}`
  (default cache TTL); the compare verdict is keyed by sha256 of the sorted
  listing-id set. All cache reads/writes are best-effort — a Redis outage
  degrades transparently to Postgres.

Compare — two entry points, on purpose (WS2 requirement)
------------------------------------------------------------------------
`POST /api/batch/compare` has always built the AI verdict: `_compare_verdict`
fans out `services.reviews.synthesize_reviews()` per listing via
`asyncio.gather`, plus one grounded verdict call — up to 5 LLM calls. WS2's
`compare_listings` MCP tool must not silently spend that quota just to let
an agent browse a comparison matrix, so this module exposes both:
  * `compare_listings()`      — matrix only, zero LLM calls.
  * `compare_listings_with_verdict()` — matrix + verdict, what the HTTP
    endpoint uses today. Behaviour there is unchanged.
"""
import asyncio
import hashlib
import json
import logging
import math
import time
from datetime import date

from .. import llm
from ..agents.retrieval import _parse_constraints
from ..availability import availability_window, is_available_range
from ..cache import cache_get, cache_set
from ..db import get_pool
from ..schemas import (
    AvailabilityDay,
    CompareMatrix,
    ListingCard,
    ListingDetail,
    SearchFilters,
    SearchResponse,
    StructuredQuery,
)
from . import reviews as reviews_service

logger = logging.getLogger(__name__)

# Key amenities to surface on a card (first 4 that exist on the listing)
_KEY_AMENITY_PRIORITY = [
    "wifi", "pool", "hot_tub", "gym", "parking", "kitchen", "ac",
    "pets_allowed", "balcony", "breakfast_included", "workspace", "bbq",
]

_AVAILABILITY_DAYS = 30  # detail-page calendar preview window


# ── Cache helpers (best-effort; a Redis outage never breaks a request) ──────

async def _cache_get_safe(key: str):
    try:
        return await cache_get(key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis cache_get failed (key=%s): %s", key, exc)
        return None


async def _cache_set_safe(key: str, value, ttl: int | None = None) -> None:
    try:
        await cache_set(key, value, ttl)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis cache_set failed (key=%s): %s", key, exc)


# ── Search ────────────────────────────────────────────────────────────────

def _search_cache_key(filters: SearchFilters) -> str:
    """Stable, normalised cache key for a filter set."""
    canonical = filters.model_dump(mode="json")
    raw = json.dumps(canonical, sort_keys=True, default=str)
    return "search:" + hashlib.sha256(raw.encode()).hexdigest()


def build_search_query(filters: SearchFilters) -> tuple[str, list, str, list]:
    """Return (count_sql, count_params, rows_sql, rows_params).

    count_sql / count_params: SELECT COUNT(*) with WHERE clause only.
    rows_sql  / rows_params : same WHERE + ORDER BY + LIMIT/OFFSET.

    The two param lists are kept separate because ORDER BY (for distance
    sort) injects lat/lng params that are not needed by the count query, so
    a simple params[:-2] slice would be wrong when distance sort is active.

    User input NEVER enters the query string — only via $N placeholders.
    """
    where_params: list = []

    def wp(value) -> str:
        where_params.append(value)
        return f"${len(where_params)}"

    where: list[str] = []

    if filters.city:
        where.append(f"LOWER(city) = LOWER({wp(filters.city)})")

    if filters.price_min is not None:
        where.append(f"base_price >= {wp(filters.price_min)}")
    if filters.price_max is not None:
        where.append(f"base_price <= {wp(filters.price_max)}")

    if filters.min_rating is not None:
        where.append(f"rating >= {wp(filters.min_rating)}")

    if filters.property_types:
        placeholders = ", ".join(wp(t) for t in filters.property_types)
        where.append(f"type IN ({placeholders})")

    # amenities — JSONB @> (AND semantics: all must be present). Each
    # amenity is checked separately so the GIN index can be used; the
    # Python list is passed straight through (JSONB codec registered on the
    # pool connection) and the ::jsonb cast tells Postgres the param type.
    for amenity in filters.amenities:
        where.append(f"amenities @> {wp([amenity])}::jsonb")

    total_guests = filters.adults + filters.children
    if total_guests > 1:
        where.append(f"beds >= {wp(total_guests)}")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    count_sql = f"SELECT COUNT(*) FROM listings {where_sql}"
    count_params = list(where_params)  # snapshot before ORDER params are added

    rows_params: list = list(where_params)
    next_idx = len(rows_params) + 1

    def rp(value) -> str:
        nonlocal next_idx
        rows_params.append(value)
        idx = next_idx
        next_idx += 1
        return f"${idx}"

    if filters.sort == "price_asc":
        order = "base_price ASC"
    elif filters.sort == "rating":
        order = "rating DESC NULLS LAST"
    elif (
        filters.sort == "distance"
        and filters.near_lat is not None
        and filters.near_lng is not None
    ):
        lat_ph = rp(filters.near_lat)
        lng_ph = rp(filters.near_lng)
        order = (
            f"(6371 * 2 * ASIN(SQRT("
            f"  POWER(SIN(RADIANS(lat - {lat_ph}) / 2), 2) + "
            f"  COS(RADIANS({lat_ph})) * COS(RADIANS(lat)) * "
            f"  POWER(SIN(RADIANS(lng - {lng_ph}) / 2), 2)"
            f"))) ASC"
        )
    else:
        order = "review_count DESC"

    offset = (filters.page - 1) * filters.page_size
    limit_ph = rp(filters.page_size)
    offset_ph = rp(offset)

    rows_sql = (
        f"SELECT id, name, type, city, neighbourhood, lat, lng, "
        f"       base_price, beds, amenities, photos, rating, review_count "
        f"FROM listings "
        f"{where_sql} "
        f"ORDER BY {order} "
        f"LIMIT {limit_ph} OFFSET {offset_ph}"
    )

    return count_sql, count_params, rows_sql, rows_params


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    )
    return round(R * 2 * math.asin(math.sqrt(a)), 2)


def row_to_card(
    row,
    check_in: date | None,
    check_out: date | None,
    near_lat: float | None,
    near_lng: float | None,
) -> tuple[ListingCard, bool]:
    """Convert a DB row to a ListingCard.

    Returns (card, available) where available=True when no date range is
    requested OR the listing passes is_available_range(). When
    available=False the caller should exclude the card.

    NOTE: is_available_range is pure Python/CPU — no I/O, no await needed.
    """
    amenities: list[str] = row["amenities"] if row["amenities"] else []
    photos: list[str] = row["photos"] if row["photos"] else []
    base_price = float(row["base_price"])

    total_for_stay: float | None = None
    available = True

    if check_in and check_out:
        available, total_for_stay = is_available_range(
            row["id"], check_in, check_out, base_price
        )
        if not available:
            return None, False  # type: ignore[return-value]

    key_amenities = [a for a in _KEY_AMENITY_PRIORITY if a in amenities][:4]

    distance_km: float | None = None
    if near_lat is not None and near_lng is not None:
        distance_km = _haversine_km(near_lat, near_lng, float(row["lat"]), float(row["lng"]))

    card = ListingCard(
        id=row["id"],
        name=row["name"],
        type=row["type"],
        city=row["city"],
        neighbourhood=row["neighbourhood"],
        lat=float(row["lat"]),
        lng=float(row["lng"]),
        price_per_night=base_price,
        total_for_stay=total_for_stay,
        rating=float(row["rating"]) if row["rating"] is not None else None,
        review_count=row["review_count"] or 0,
        key_amenities=key_amenities,
        photo=photos[0] if photos else None,
        distance_km=distance_km,
    )
    return card, True


async def search_listings(filters: SearchFilters) -> SearchResponse:
    """Run a filtered/sorted search against Postgres listings.

    Filters applied in SQL: city, price range, min rating, property types,
    amenities (GIN JSONB @>), guest count (beds proxy).

    Availability filter (check_in/check_out): applied in Python AFTER the
    DB page is fetched. total reflects pre-availability count (documented
    limitation — acceptable at this scale).

    Sorting: price_asc, rating, popularity (review_count), distance
    (haversine in SQL when near_lat/near_lng provided).
    """
    t0 = time.perf_counter()
    cache_key = _search_cache_key(filters)

    cached = await _cache_get_safe(cache_key)
    if cached:
        logger.info("search cache hit (key=%.16s)", cache_key)
        return SearchResponse(**cached)

    pool = await get_pool()
    count_sql, count_params, rows_sql, rows_params = build_search_query(filters)

    async with pool.acquire() as conn:
        total: int = await conn.fetchval(count_sql, *count_params)
        rows = await conn.fetch(rows_sql, *rows_params)

    check_in = filters.check_in
    check_out = filters.check_out
    near_lat = filters.near_lat
    near_lng = filters.near_lng

    # Availability is pure CPU — run synchronously (no blocking I/O)
    cards: list[ListingCard] = []
    for row in rows:
        card, ok = row_to_card(row, check_in, check_out, near_lat, near_lng)
        if ok:
            cards.append(card)

    response = SearchResponse(
        results=cards,
        total=total,
        page=filters.page,
        page_size=filters.page_size,
    )

    elapsed_ms = (time.perf_counter() - t0) * 1000
    logger.info(
        "search total=%d returned=%d page=%d elapsed_ms=%.1f",
        total, len(cards), filters.page, elapsed_ms,
    )

    # Cache — short TTL for search results (data changes as ingestion runs)
    await _cache_set_safe(cache_key, response.model_dump(mode="json"), ttl=120)

    return response


async def fallback_search_candidates(
    filters: SearchFilters, limit: int = 10
) -> list[ListingCard]:
    """Best-effort listing fetch with NO caching and NO availability filter.

    Used exclusively by `agents/orchestrator.py::_fallback_search` for
    graceful degradation when semantic retrieval fails — a rough
    traditional result set beats an empty stream. Deliberately skips the
    cache (this is already the degraded path) and passes check_in/check_out
    as None to `row_to_card` so a date range never drops results here,
    matching the pre-extraction behaviour of `_fallback_search` exactly.
    """
    pool = await get_pool()
    _, _, rows_sql, rows_params = build_search_query(filters)
    async with pool.acquire() as conn:
        rows = await conn.fetch(rows_sql, *rows_params)

    cards: list[ListingCard] = []
    for row in rows[:limit]:
        card, ok = row_to_card(row, None, None, None, None)
        if ok:
            cards.append(card)
    return cards


def apply_constraint_filters(sq: StructuredQuery, filters: SearchFilters) -> SearchFilters:
    """Overlay amenities/property_type onto `filters`.

    Parses `sq.hard_constraints`/`soft_preferences` via the retrieval
    agent's constraint parser so the NL-search filter chips and the actual
    search agree on what was understood. Moved here (from a mid-function
    `agents.retrieval` import inside `routers/agents.py::nl_search`) so a
    router never reaches into an agent's private helper directly — the
    service layer does, which is the sanctioned direction.
    """
    parsed = _parse_constraints(sq)
    property_types = [parsed["property_type"]] if parsed["property_type"] else []
    return SearchFilters(
        **{
            **filters.model_dump(),
            "amenities": parsed["amenities"],
            "property_types": property_types,
        }
    )


# ── Listing detail ───────────────────────────────────────────────────────────

_DETAIL_COLUMNS_SQL = """
SELECT
    l.id, l.name, l.type, l.city, l.neighbourhood,
    l.lat, l.lng, l.base_price, l.beds,
    l.amenities, l.photos, l.host,
    l.rating, l.review_count, l.neighbourhood_price_pct,
    ls.summary, ls.aspect_avg,
    -- Deliberately NOT `ls.provenance`. There is no migration tool here — the
    -- schema is applied by ingestion — so a database provisioned before WS0-A
    -- has no such column and a direct reference would make every listing-detail
    -- request fail with UndefinedColumn on deploy. `to_jsonb(row)->>'key'`
    -- yields NULL for an absent key instead of raising, and NULL is exactly the
    -- right answer: a database without the column also has no LLM summaries, so
    -- the UI correctly falls back to the "quoted from reviews" label.
    to_jsonb(ls) ->> 'provenance' AS summary_provenance
FROM listings l
LEFT JOIN listing_summaries ls ON ls.listing_id = l.id
"""

_DETAIL_SQL = _DETAIL_COLUMNS_SQL + "WHERE l.id = $1"


def _row_to_detail(row, cal: list[dict]) -> ListingDetail:
    return ListingDetail(
        id=row["id"],
        name=row["name"],
        type=row["type"],
        city=row["city"],
        neighbourhood=row["neighbourhood"],
        lat=float(row["lat"]),
        lng=float(row["lng"]),
        base_price=float(row["base_price"]),
        beds=row["beds"],
        amenities=list(row["amenities"]) if row["amenities"] else [],
        photos=list(row["photos"]) if row["photos"] else [],
        host=dict(row["host"]) if row["host"] else {},
        rating=float(row["rating"]) if row["rating"] is not None else None,
        review_count=row["review_count"] or 0,
        neighbourhood_price_pct=(
            float(row["neighbourhood_price_pct"])
            if row["neighbourhood_price_pct"] is not None
            else None
        ),
        summary=row["summary"],
        # WS0-A: the UI labels this "AI Review Summary". Most rows are still
        # extractive review quotes, so the label needs to know which it has.
        # Always present in the result set (see the SQL), NULL on an unmigrated DB.
        summary_provenance=row["summary_provenance"],
        aspect_avg=dict(row["aspect_avg"]) if row["aspect_avg"] else None,
        availability_window=[AvailabilityDay(**d) for d in cal],
    )


async def get_listing_detail(listing_id: str) -> ListingDetail | None:
    """Property detail: full listing row + AI summary + 30-day calendar
    preview, or None if the listing does not exist (the router raises 404).

    The calendar preview is computed from the deterministic availability
    function — no extra DB query needed. `listing_summaries` rows may not
    yet exist while ingestion is running; the result still returns in that
    case (summary/aspect_avg will be None).
    """
    cache_key = f"listing:{listing_id}"
    cached = await _cache_get_safe(cache_key)
    if cached:
        return ListingDetail(**cached)

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_DETAIL_SQL, listing_id)

    if row is None:
        return None

    base_price = float(row["base_price"])
    today = date.today()
    cal = availability_window(listing_id, today, _AVAILABILITY_DAYS, base_price)
    detail = _row_to_detail(row, cal)

    await _cache_set_safe(cache_key, detail.model_dump(mode="json"))
    return detail


# ── Compare ───────────────────────────────────────────────────────────────

_VERDICT_SYSTEM = (
    "You are a concise travel comparison analyst. Give a 2-4 sentence verdict "
    "grounded STRICTLY in the provided facts and review summaries — never invent "
    "details. Refer to properties by name, keep prices in their stated currency, "
    "and say which is the best overall value, which has the strongest reviews, and "
    "which suits which kind of traveler."
)


def _ccy(city: str | None) -> str:
    """Currency symbol per city (Amsterdam/Lisbon = EUR, Los Angeles = USD)."""
    return "€" if city in ("Amsterdam", "Lisbon") else "$"


async def fetch_listings_for_compare(listing_ids: list[str]) -> list[ListingDetail]:
    """Fetch requested listings in a single IN-query (no N+1).

    Preserves the requested order; ids not found are silently skipped
    (partial match is acceptable — matches the pre-extraction behaviour).
    """
    if not listing_ids:
        return []

    placeholders = ", ".join(f"${i + 1}" for i in range(len(listing_ids)))
    sql = _DETAIL_COLUMNS_SQL + f"WHERE l.id IN ({placeholders})"

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *listing_ids)

    row_map = {row["id"]: row for row in rows}
    today = date.today()
    out: list[ListingDetail] = []
    for lid in listing_ids:
        row = row_map.get(lid)
        if row is None:
            continue  # Silently skip IDs not found — partial match is acceptable
        base_price = float(row["base_price"])
        cal = availability_window(lid, today, _AVAILABILITY_DAYS, base_price)
        out.append(_row_to_detail(row, cal))
    return out


async def _compare_verdict(listings: list[ListingDetail]) -> str | None:
    """AI verdict for the compare page.

    Runs per-listing review synthesis IN PARALLEL (asyncio.gather — this is
    the brief's "batch endpoint, parallel synthesis"), then a single
    grounded LLM verdict over the facts + syntheses. Cached by the listing
    set; degrades to None (matrix-only) on LLM failure.
    """
    if len(listings) < 2:
        return None

    cache_key = "verdict:" + hashlib.sha256(
        ",".join(sorted(item.id for item in listings)).encode()
    ).hexdigest()
    try:
        cached = await cache_get(cache_key)
        if cached:
            return cached.get("verdict")
    except Exception:  # noqa: BLE001
        pass

    # Parallel grounded review synthesis per listing, through the review
    # service so the abstention contract (WS0-C) is applied consistently —
    # though here we only need the synthesis text, same as before.
    results = await asyncio.gather(
        *[reviews_service.synthesize_reviews([item.id], focus=None) for item in listings],
        return_exceptions=True,
    )

    blocks: list[str] = []
    for item, res in zip(listings, results):
        review_text = res["text"] if isinstance(res, dict) else (item.summary or "no review summary")
        amenities = ", ".join(item.amenities[:8]) if item.amenities else "—"
        blocks.append(
            f"- {item.name} ({item.city}{', ' + item.neighbourhood if item.neighbourhood else ''}): "
            f"{_ccy(item.city)}{round(item.base_price)}/night, rating {item.rating if item.rating is not None else 'n/a'} "
            f"({item.review_count} reviews), {item.beds} bed(s). Amenities: {amenities}. "
            f"Reviews: {review_text}"
        )
    prompt = "Compare these stays and give your verdict.\n\n" + "\n".join(blocks)

    try:
        verdict = await llm.complete_text(prompt, _VERDICT_SYSTEM)
    except llm.LLMError as exc:
        logger.warning("compare verdict LLM failed: %s", exc)
        return None

    try:
        await cache_set(cache_key, {"verdict": verdict})
    except Exception:  # noqa: BLE001
        pass
    return verdict


async def compare_listings(listing_ids: list[str]) -> list[ListingDetail]:
    """Comparison matrix only — zero LLM calls.

    Use this for any browsing caller (WS2's `compare_listings` MCP tool, or
    any future caller) that must not silently spend quota. The HTTP
    endpoint (`POST /api/batch/compare`) wants the verdict too and calls
    `compare_listings_with_verdict` instead.
    """
    return await fetch_listings_for_compare(listing_ids)


async def compare_listings_with_verdict(listing_ids: list[str]) -> CompareMatrix:
    """Matrix + AI verdict — up to 5 LLM calls (per-listing review synthesis
    fanned out via asyncio.gather, plus one grounded verdict call).
    Preserves `POST /api/batch/compare`'s existing behaviour exactly.
    """
    listings_out = await fetch_listings_for_compare(listing_ids)
    verdict = await _compare_verdict(listings_out)
    return CompareMatrix(listings=listings_out, verdict=verdict)
