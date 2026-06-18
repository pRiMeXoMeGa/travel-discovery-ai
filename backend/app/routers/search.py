"""Traditional search/filter endpoints (brief §2.2 / §2.4).

Design notes
------------
* SQL is built by accumulating parameterized clauses into a list — user input
  NEVER reaches the query string (only via $N placeholders with asyncpg).
* Pagination is entirely at the DB level (LIMIT / OFFSET); we never load all
  rows into Python memory.
* Availability filter: when check_in/check_out are supplied, we first let SQL
  do all the cheap filters and then apply is_available_range() in Python on
  the returned page.  This means the availability filter applies AFTER DB
  pagination, so `total` reflects pre-availability counts.  This is an
  accepted simplification at the current scale (~1K listings); a future
  materialised-calendar table would fix it.
* Distance: when near_lat/near_lng are provided the haversine formula is
  computed inline in SQL (works fine for a few thousand rows; no PostGIS needed).
* Beds-as-capacity simplification: `guests` isn't a column; we use `beds` as a
  proxy for maximum capacity.  When `adults + children > beds` we include
  listings with beds >= party_size.  Documented limitation: hosts may list beds
  differently from true capacity.
* Redis caching: cache key = sha256 of the canonical JSON of SearchFilters.
  TTL defaults to settings.cache_ttl_seconds.  If Redis is unreachable the
  exception is caught and the request falls through to Postgres.
"""
import hashlib
import json
import logging
import math
import time
from datetime import date

from fastapi import APIRouter

from ..availability import is_available_range
from ..cache import cache_get, cache_set
from ..db import get_pool
from ..schemas import ListingCard, SearchFilters, SearchResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["search"])

# Key amenities to surface on the card (first 4 that exist on the listing)
_KEY_AMENITY_PRIORITY = [
    "wifi", "pool", "hot_tub", "gym", "parking", "kitchen", "ac",
    "pets_allowed", "balcony", "breakfast_included", "workspace", "bbq",
]


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _cache_key(filters: SearchFilters) -> str:
    """Stable, normalised cache key for a filter set."""
    canonical = filters.model_dump(mode="json")
    raw = json.dumps(canonical, sort_keys=True, default=str)
    return "search:" + hashlib.sha256(raw.encode()).hexdigest()


async def _cache_get_safe(key: str):
    """cache_get wrapped so a Redis outage doesn't break search."""
    try:
        return await cache_get(key)
    except Exception as exc:
        logger.warning("Redis cache_get failed (key=%s): %s", key, exc)
        return None


async def _cache_set_safe(key: str, value, ttl: int | None = None) -> None:
    try:
        await cache_set(key, value, ttl)
    except Exception as exc:
        logger.warning("Redis cache_set failed (key=%s): %s", key, exc)


# ── SQL builder ───────────────────────────────────────────────────────────────

def _build_query(filters: SearchFilters) -> tuple[str, list, str, list]:
    """Return (count_sql, count_params, rows_sql, rows_params).

    count_sql / count_params: SELECT COUNT(*) with WHERE clause only.
    rows_sql  / rows_params : same WHERE + ORDER BY + LIMIT/OFFSET.

    The two param lists are kept separate because ORDER BY (for distance sort)
    injects lat/lng params that are not needed by the count query, so a simple
    params[:-2] slice would be wrong when distance sort is active.

    User input NEVER enters the query string — only via $N placeholders.
    """
    # WHERE params shared by both queries
    where_params: list = []

    def wp(value) -> str:
        """Append to where_params, return $N."""
        where_params.append(value)
        return f"${len(where_params)}"

    where: list[str] = []

    # city filter
    if filters.city:
        where.append(f"LOWER(city) = LOWER({wp(filters.city)})")

    # price range
    if filters.price_min is not None:
        where.append(f"base_price >= {wp(filters.price_min)}")
    if filters.price_max is not None:
        where.append(f"base_price <= {wp(filters.price_max)}")

    # minimum rating
    if filters.min_rating is not None:
        where.append(f"rating >= {wp(filters.min_rating)}")

    # property types (IN list)
    if filters.property_types:
        placeholders = ", ".join(wp(t) for t in filters.property_types)
        where.append(f"type IN ({placeholders})")

    # amenities — JSONB @> (AND semantics: all must be present)
    # Each amenity string is checked separately so we can use the GIN index.
    # We pass a Python list directly; with the JSONB codec registered on the
    # pool connection, asyncpg serializes it to JSON correctly without double-
    # encoding.  The ::jsonb cast tells Postgres the parameter type.
    for amenity in filters.amenities:
        where.append(f"amenities @> {wp([amenity])}::jsonb")

    # guest count — beds as proxy for capacity (documented simplification)
    total_guests = filters.adults + filters.children
    if total_guests > 1:
        where.append(f"beds >= {wp(total_guests)}")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    count_sql    = f"SELECT COUNT(*) FROM listings {where_sql}"
    count_params = list(where_params)  # snapshot before ORDER params are added

    # ── ORDER BY ──
    # rows_params starts as a copy of where_params; ORDER BY may append more.
    rows_params: list = list(where_params)
    next_idx = len(rows_params) + 1  # next $N index for rows-only params

    def rp(value) -> str:
        """Append to rows_params, return the next $N."""
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
        # Haversine in SQL — no PostGIS required; works fine at ~1K rows.
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

    offset    = (filters.page - 1) * filters.page_size
    limit_ph  = rp(filters.page_size)
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


# ── Haversine (Python, for distance_km on result cards) ──────────────────────

def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    return round(R * 2 * math.asin(math.sqrt(a)), 2)


# ── Row → ListingCard ─────────────────────────────────────────────────────────

def _row_to_card(
    row,
    check_in: date | None,
    check_out: date | None,
    near_lat: float | None,
    near_lng: float | None,
) -> tuple[ListingCard, bool]:
    """Convert a DB row to a ListingCard.

    Returns (card, available) where available=True when no date range is
    requested OR the listing passes is_available_range().  When available=False
    the caller should exclude the card.

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

    # Pick a representative subset of amenities for the card
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


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.post("/search", response_model=SearchResponse)
async def search(filters: SearchFilters) -> SearchResponse:
    """Run a filtered/sorted search against Postgres listings.

    Filters applied in SQL: city, price range, min rating, property types,
    amenities (GIN JSONB @>), guest count (beds proxy).

    Availability filter (check_in/check_out): applied in Python AFTER the DB
    page is fetched.  total reflects pre-availability count (documented
    limitation — acceptable at this scale).

    Sorting: price_asc, rating, popularity (review_count), distance (haversine
    in SQL when near_lat/near_lng provided).
    """
    t0 = time.perf_counter()
    cache_key = _cache_key(filters)

    cached = await _cache_get_safe(cache_key)
    if cached:
        logger.info("search cache hit (key=%.16s)", cache_key)
        return SearchResponse(**cached)

    pool = await get_pool()
    count_sql, count_params, rows_sql, rows_params = _build_query(filters)

    async with pool.acquire() as conn:
        total: int = await conn.fetchval(count_sql, *count_params)
        rows       = await conn.fetch(rows_sql, *rows_params)

    check_in  = filters.check_in
    check_out = filters.check_out
    near_lat  = filters.near_lat
    near_lng  = filters.near_lng

    # Availability is pure CPU — run synchronously (no blocking I/O)
    cards: list[ListingCard] = []
    for row in rows:
        card, ok = _row_to_card(row, check_in, check_out, near_lat, near_lng)
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
