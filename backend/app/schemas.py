"""Pydantic models shared across the API."""
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


# ── Search / filter (traditional surface, brief §2.2) ────────────────────────
class SearchFilters(BaseModel):
    city: str | None = None
    check_in: date | None = None
    check_out: date | None = None
    adults: int = 1
    children: int = 0
    rooms: int = 1
    price_min: float | None = None
    price_max: float | None = None
    min_rating: float | None = None
    property_types: list[str] = Field(default_factory=list)
    amenities: list[str] = Field(default_factory=list)
    sort: Literal["price_asc", "rating", "popularity", "distance"] = "popularity"
    # Reference point for distance sort / "near X".
    near_lat: float | None = None
    near_lng: float | None = None
    page: int = 1
    page_size: int = 24


class ListingCard(BaseModel):
    id: str
    name: str
    type: str
    city: str
    neighbourhood: str | None = None
    lat: float
    lng: float
    price_per_night: float
    total_for_stay: float | None = None
    rating: float | None = None
    review_count: int = 0
    key_amenities: list[str] = Field(default_factory=list)
    photo: str | None = None
    distance_km: float | None = None


class SearchResponse(BaseModel):
    results: list[ListingCard]
    total: int
    page: int
    page_size: int


# ── Agents (brief §2.3) ───────────────────────────────────────────────────────
class StructuredQuery(BaseModel):
    """Intent agent output: NL -> structured query."""
    city: str | None = None
    check_in: date | None = None
    check_out: date | None = None
    party_size: int | None = None
    budget_per_night: float | None = None
    budget_total: float | None = None
    hard_constraints: list[str] = Field(default_factory=list)
    soft_preferences: list[str] = Field(default_factory=list)
    vibe: str | None = None


class ConciergeRequest(BaseModel):
    query: str
    filters: SearchFilters | None = None


class Citation(BaseModel):
    kind: Literal["listing", "review"]
    id: str
    snippet: str | None = None


class CompareRequest(BaseModel):
    listing_ids: list[str] = Field(min_length=2, max_length=4)
