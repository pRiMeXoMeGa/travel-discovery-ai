"""Listing detail + reviews + batch endpoints (brief §2.2 / §2.4).

Design notes
------------
* GET /api/listings/{id}: joins listing + listing_summaries in one DB round-trip
  (LEFT JOIN so detail still works when the summary hasn't been computed yet).
  Returns the full listing detail including a 30-day availability calendar
  preview computed from the deterministic function.
* GET /api/listings/{id}/reviews: paginated reviews with optional filters on
  language, min_score (rating), and topic (full-text search in `text`).
  Aspects JSONB is returned as-is.
* POST /api/batch/compare: fetches all listings in a single IN-query (no N+1),
  returns the comparison matrix.  The AI verdict is deferred to the agent phase
  and returns null for now.
* Redis caching: detail pages are cached with a longer TTL (settings default
  3600s); review pages use 120s.  Both fall back gracefully on Redis failure.
"""
import asyncio
import hashlib
import json
import logging
import time
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException

from .. import llm
from ..agents import review_intel
from ..availability import availability_window
from ..cache import cache_get, cache_set
from ..db import get_pool
from ..schemas import (
    AvailabilityDay,
    CompareMatrix,
    CompareRequest,
    ListingDetail,
    ReviewItem,
    ReviewsResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["listings"])

_REVIEWS_PAGE_SIZE = 20
_AVAILABILITY_DAYS  = 30


# ── Cache helpers ─────────────────────────────────────────────────────────────

async def _cache_get_safe(key: str):
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


# ── Detail ────────────────────────────────────────────────────────────────────

_DETAIL_SQL = """
SELECT
    l.id, l.name, l.type, l.city, l.neighbourhood,
    l.lat, l.lng, l.base_price, l.beds,
    l.amenities, l.photos, l.host,
    l.rating, l.review_count, l.neighbourhood_price_pct,
    ls.summary, ls.aspect_avg
FROM listings l
LEFT JOIN listing_summaries ls ON ls.listing_id = l.id
WHERE l.id = $1
"""


@router.get("/listings/{listing_id}", response_model=ListingDetail)
async def get_listing(listing_id: str) -> ListingDetail:
    """Property detail: full listing row + AI summary + 30-day calendar preview.

    The calendar preview is computed from the deterministic availability
    function — no extra DB query needed.  listing_summaries rows may not yet
    exist while ingestion is running; the endpoint still returns data in that
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
        raise HTTPException(status_code=404, detail=f"Listing '{listing_id}' not found")

    base_price = float(row["base_price"])
    today = date.today()
    cal = availability_window(listing_id, today, _AVAILABILITY_DAYS, base_price)

    detail = ListingDetail(
        id=row["id"],
        name=row["name"],
        type=row["type"],
        city=row["city"],
        neighbourhood=row["neighbourhood"],
        lat=float(row["lat"]),
        lng=float(row["lng"]),
        base_price=base_price,
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
        aspect_avg=dict(row["aspect_avg"]) if row["aspect_avg"] else None,
        availability_window=[AvailabilityDay(**d) for d in cal],
    )

    await _cache_set_safe(cache_key, detail.model_dump(mode="json"))
    return detail


# ── Reviews ───────────────────────────────────────────────────────────────────

def _build_reviews_query(
    listing_id: str,
    language: str | None,
    min_score: float | None,
    topic: str | None,
    page: int,
    page_size: int,
) -> tuple[str, str, list]:
    """Build parametrized count + rows queries for reviews."""
    params: list = [listing_id]

    def p(value) -> str:
        params.append(value)
        return f"${len(params)}"

    where = ["listing_id = $1"]

    if language:
        where.append(f"language = {p(language)}")
    if min_score is not None:
        where.append(f"rating >= {p(min_score)}")
    if topic:
        # Simple full-text containment — no FTS index required at this scale
        where.append(f"text ILIKE {p('%' + topic + '%')}")

    where_sql = "WHERE " + " AND ".join(where)
    offset = (page - 1) * page_size

    count_sql = f"SELECT COUNT(*) FROM reviews {where_sql}"
    rows_sql = (
        f"SELECT id, date, reviewer, rating, text, language, aspects, sentiment "
        f"FROM reviews {where_sql} "
        f"ORDER BY date DESC NULLS LAST "
        f"LIMIT {p(page_size)} OFFSET {p(offset)}"
    )
    return count_sql, rows_sql, params


@router.get("/listings/{listing_id}/reviews", response_model=ReviewsResponse)
async def get_reviews(
    listing_id: str,
    language: str | None = None,
    min_score: float | None = None,
    topic: str | None = None,
    page: int = 1,
) -> ReviewsResponse:
    """Paginated, filterable reviews.

    Filters: language (exact), min_score (rating >=), topic (ILIKE on text).
    Results ordered newest-first.  Aspects JSONB included for the UI.
    """
    page_size = _REVIEWS_PAGE_SIZE

    # Verify listing exists
    pool = await get_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM listings WHERE id = $1", listing_id
        )
        if not exists:
            raise HTTPException(status_code=404, detail=f"Listing '{listing_id}' not found")

        count_sql, rows_sql, params = _build_reviews_query(
            listing_id, language, min_score, topic, page, page_size
        )
        total: int = await conn.fetchval(count_sql, *params[:-2])
        rows       = await conn.fetch(rows_sql, *params)

    items = [
        ReviewItem(
            id=row["id"],
            date=row["date"].isoformat() if row["date"] else None,
            reviewer=row["reviewer"],
            rating=float(row["rating"]) if row["rating"] is not None else None,
            text=row["text"],
            language=row["language"],
            aspects=dict(row["aspects"]) if row["aspects"] else None,
            sentiment=float(row["sentiment"]) if row["sentiment"] is not None else None,
        )
        for row in rows
    ]

    return ReviewsResponse(results=items, total=total, page=page, page_size=page_size)


# ── Batch compare ─────────────────────────────────────────────────────────────

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


async def _compare_verdict(listings: list[ListingDetail]) -> str | None:
    """AI verdict for the compare page.

    Runs per-listing review synthesis IN PARALLEL (asyncio.gather — this is the
    brief's "batch endpoint, parallel synthesis"), then a single grounded LLM
    verdict over the facts + syntheses. Cached by the listing set; degrades to
    None (matrix-only) on LLM failure.
    """
    if len(listings) < 2:
        return None

    cache_key = "verdict:" + hashlib.sha256(
        ",".join(sorted(l.id for l in listings)).encode()
    ).hexdigest()
    try:
        cached = await cache_get(cache_key)
        if cached:
            return cached.get("verdict")
    except Exception:  # noqa: BLE001
        pass

    # Parallel grounded review synthesis per listing.
    syntheses = await asyncio.gather(
        *[review_intel.synthesize([l.id], focus=None) for l in listings],
        return_exceptions=True,
    )

    blocks: list[str] = []
    for l, syn in zip(listings, syntheses):
        review_text = syn[0] if isinstance(syn, tuple) else (l.summary or "no review summary")
        amenities = ", ".join(l.amenities[:8]) if l.amenities else "—"
        blocks.append(
            f"- {l.name} ({l.city}{', ' + l.neighbourhood if l.neighbourhood else ''}): "
            f"{_ccy(l.city)}{round(l.base_price)}/night, rating {l.rating if l.rating is not None else 'n/a'} "
            f"({l.review_count} reviews), {l.beds} bed(s). Amenities: {amenities}. "
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


@router.post("/batch/compare", response_model=CompareMatrix)
async def compare(req: CompareRequest) -> CompareMatrix:
    """Fetch 2–4 listings in a single query and return a comparison matrix
    plus an AI verdict built from PARALLEL per-listing review synthesis."""
    ids = req.listing_ids
    if not (2 <= len(ids) <= 4):
        raise HTTPException(status_code=422, detail="Provide between 2 and 4 listing IDs")

    # Build a single parameterised IN-query — never N+1
    placeholders = ", ".join(f"${i + 1}" for i in range(len(ids)))
    sql = f"""
        SELECT
            l.id, l.name, l.type, l.city, l.neighbourhood,
            l.lat, l.lng, l.base_price, l.beds,
            l.amenities, l.photos, l.host,
            l.rating, l.review_count, l.neighbourhood_price_pct,
            ls.summary, ls.aspect_avg
        FROM listings l
        LEFT JOIN listing_summaries ls ON ls.listing_id = l.id
        WHERE l.id IN ({placeholders})
    """

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *ids)

    if len(rows) == 0:
        raise HTTPException(status_code=404, detail="No listings found for the given IDs")

    # Index by id so we can preserve the requested order
    row_map = {row["id"]: row for row in rows}

    today = date.today()
    listings_out: list[ListingDetail] = []
    for lid in ids:
        row = row_map.get(lid)
        if row is None:
            continue  # Silently skip IDs not found — partial match is acceptable
        base_price = float(row["base_price"])
        cal = availability_window(lid, today, _AVAILABILITY_DAYS, base_price)
        listings_out.append(
            ListingDetail(
                id=row["id"],
                name=row["name"],
                type=row["type"],
                city=row["city"],
                neighbourhood=row["neighbourhood"],
                lat=float(row["lat"]),
                lng=float(row["lng"]),
                base_price=base_price,
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
                aspect_avg=dict(row["aspect_avg"]) if row["aspect_avg"] else None,
                availability_window=[AvailabilityDay(**d) for d in cal],
            )
        )

    verdict = await _compare_verdict(listings_out)
    return CompareMatrix(listings=listings_out, verdict=verdict)
