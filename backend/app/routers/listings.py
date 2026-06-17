"""Listing detail + batch endpoints (brief §2.2 / §2.4)."""
from fastapi import APIRouter

from ..schemas import CompareRequest

router = APIRouter(prefix="/api", tags=["listings"])


@router.get("/listings/{listing_id}")
async def get_listing(listing_id: str) -> dict:
    """Property detail: gallery, amenities, aspect scores, AI summary,
    availability (deterministic calendar), and price-breakdown data.

    TODO: join listing row + precomputed summary + aspect sentiment + reviews.
    """
    raise NotImplementedError("TODO: listing detail")


@router.get("/listings/{listing_id}/reviews")
async def get_reviews(
    listing_id: str,
    language: str | None = None,
    min_score: float | None = None,
    topic: str | None = None,
    page: int = 1,
) -> dict:
    """Filterable reviews (by language / score / topic) with aspect scores."""
    raise NotImplementedError("TODO: reviews query")


@router.post("/batch/compare")
async def compare(req: CompareRequest) -> dict:
    """Batch endpoint: compare 2–4 listings (price, amenities, AI verdict).

    TODO: fetch listings in one query; run review syntheses in PARALLEL
    (asyncio.gather) for the AI verdict; assemble the comparison matrix.
    """
    raise NotImplementedError("TODO: batch compare")
