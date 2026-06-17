"""Traditional search/filter endpoints (brief §2.2 / §2.4).

These run ALONGSIDE the agent system and power the booking surface: date-range
availability, guest/price/rating/type/amenity filters, and sorting.
"""
from fastapi import APIRouter

from ..schemas import SearchFilters, SearchResponse

router = APIRouter(prefix="/api", tags=["search"])


@router.post("/search", response_model=SearchResponse)
async def search(filters: SearchFilters) -> SearchResponse:
    """Run a filtered/sorted search.

    TODO:
      - build a parametrized SQL query from `filters` (price/type/amenities/rating).
      - availability awareness: filter by the deterministic calendar function
        over [check_in, check_out).
      - distance sort: order by haversine to (near_lat, near_lng).
      - compute total_for_stay = nights * nightly rate.
      - paginate at the DB level (LIMIT/OFFSET) — never load all rows.
    """
    raise NotImplementedError("TODO: search query")
