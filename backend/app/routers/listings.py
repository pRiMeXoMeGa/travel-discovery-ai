"""Listing detail + reviews + batch endpoints (brief §2.2 / §2.4).

Detail and compare are thin HTTP wrappers around `app/services/listings.py`
(WS0-C service-layer extraction) — see that module for the SQL/caching/
compare-verdict design notes. The reviews-pagination endpoint below is
unrelated to that extraction (plain Postgres pagination, no cross-module
reach) and is unchanged.
"""
import logging

from fastapi import APIRouter, HTTPException

from ..db import get_pool
from ..schemas import (
    CompareMatrix,
    CompareRequest,
    ListingDetail,
    ReviewItem,
    ReviewsResponse,
)
from ..services import listings as listings_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["listings"])

_REVIEWS_PAGE_SIZE = 20


# ── Detail ────────────────────────────────────────────────────────────────────

@router.get("/listings/{listing_id}", response_model=ListingDetail)
async def get_listing(listing_id: str) -> ListingDetail:
    """Property detail: full listing row + AI summary + 30-day calendar preview.

    See `services.listings.get_listing_detail` for the query/caching design.
    `listing_summaries` rows may not yet exist while ingestion is running;
    the endpoint still returns data in that case (summary/aspect_avg will
    be None).
    """
    detail = await listings_service.get_listing_detail(listing_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Listing '{listing_id}' not found")
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
    Results ordered newest-first. Aspects JSONB included for the UI.
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
        rows = await conn.fetch(rows_sql, *params)

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

@router.post("/batch/compare", response_model=CompareMatrix)
async def compare(req: CompareRequest) -> CompareMatrix:
    """Fetch 2-4 listings in a single query and return a comparison matrix
    plus an AI verdict built from PARALLEL per-listing review synthesis.

    See `services.listings.compare_listings_with_verdict` (the verdict path
    used here) and `services.listings.compare_listings` (the verdict-free
    path — used by browsing callers such as the WS2 `compare_listings` MCP
    tool, which must never silently spend LLM quota).
    """
    ids = req.listing_ids
    if not (2 <= len(ids) <= 4):
        raise HTTPException(status_code=422, detail="Provide between 2 and 4 listing IDs")

    matrix = await listings_service.compare_listings_with_verdict(ids)
    if not matrix.listings:
        raise HTTPException(status_code=404, detail="No listings found for the given IDs")
    return matrix
