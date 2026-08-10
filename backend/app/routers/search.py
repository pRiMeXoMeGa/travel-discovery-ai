"""Traditional search/filter endpoint (brief §2.2 / §2.4).

Thin HTTP wrapper (WS0-C service-layer extraction) — all SQL construction,
caching, and availability-filter logic now live in
`app/services/listings.py`, shared with `routers/agents.py`'s NL-search
endpoint and `agents/orchestrator.py`'s graceful-degradation fallback. See
`services/listings.py` for the full design notes.
"""
from fastapi import APIRouter

from ..schemas import SearchFilters, SearchResponse
from ..services import listings as listings_service

router = APIRouter(prefix="/api", tags=["search"])


@router.post("/search", response_model=SearchResponse)
async def search(filters: SearchFilters) -> SearchResponse:
    """Run a filtered/sorted search against Postgres listings.

    See `services.listings.search_listings` for the implementation (SQL
    construction, caching, availability-filter timing, sorting).
    """
    return await listings_service.search_listings(filters)
