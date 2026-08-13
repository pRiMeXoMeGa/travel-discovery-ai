"""Pydantic models shared across the API."""
from datetime import date
from typing import Any, Literal

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
    # EVAL Q4: a family request ranks whole units first. A preference, not a
    # filter — see build_search_query. Never set from raw user input.
    prefer_whole_unit: bool = False
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
class Dealbreaker(BaseModel):
    """A standing rule that becomes a hard Qdrant payload filter.

    Captured at *write* time by the intent agent, while the sentence is still
    visible, because polarity cannot be recovered later: `pets_allowed` means
    "pets are permitted", so an allergy sufferer needs `must_not` on the same
    field a dog owner needs `must` on. Scanning remembered text for vocabulary
    terms misfires the same way — "the elevator was broken, avoid this place"
    would read as *require* elevator.

    `field`/`value` are validated against the closed payload vocabulary by
    `app.memory.store.validate_dealbreaker`; anything that fails validation is
    demoted to a soft preference rather than silently dropped.
    """
    field: Literal["amenities", "type"]
    value: str
    op: Literal["must", "must_not"]


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
    # WS1: standing rules stated in *this* turn. Empty on every pre-memory path,
    # so existing callers are unaffected.
    dealbreakers: list[Dealbreaker] = Field(default_factory=list)
    # Rules the turn revokes ("actually, shared rooms are fine now"). Free text —
    # matched against stored dealbreakers by the store, not used as a filter.
    suppress_dealbreakers: list[str] = Field(default_factory=list)
    # EVAL Q6: parts of the request no field here can express — "a castle on the
    # moon" keeps its price cap and silently loses everything else. The concierge
    # can say so in prose; `/api/nl-search` has no answer agent, so without this
    # the drop is invisible on that surface. Disclosure only: never a filter.
    unsupported: list[str] = Field(default_factory=list)


class ConciergeRequest(BaseModel):
    query: str
    filters: SearchFilters | None = None
    # WS1 identity. A localStorage UUID minted by the browser: same-browser
    # persistence, NOT authentication — the README must say so plainly.
    user_id: str | None = None
    trip_id: str | None = None


class Citation(BaseModel):
    kind: Literal["listing", "review"]
    id: str
    snippet: str | None = None


class CompareRequest(BaseModel):
    listing_ids: list[str] = Field(min_length=2, max_length=4)


# ── Listing detail ────────────────────────────────────────────────────────────
class AvailabilityDay(BaseModel):
    date: str          # ISO-8601 "YYYY-MM-DD"
    available: bool
    price: float


class ListingDetail(BaseModel):
    id: str
    name: str
    type: str
    city: str
    neighbourhood: str | None = None
    lat: float
    lng: float
    base_price: float
    beds: int
    amenities: list[str]
    photos: list[str]
    host: dict[str, Any]
    rating: float | None = None
    review_count: int = 0
    neighbourhood_price_pct: float | None = None
    # Precomputed summary (from listing_summaries table — may be null while ingestion runs)
    summary: str | None = None
    # "llm" = genuinely model-written; "heuristic" = two extractive review
    # quotes. The frontend must not call the second one AI-generated.
    summary_provenance: str | None = None
    aspect_avg: dict[str, Any] | None = None
    # 30-day availability calendar preview
    availability_window: list[AvailabilityDay] = Field(default_factory=list)


# ── Reviews ───────────────────────────────────────────────────────────────────
class ReviewItem(BaseModel):
    id: str
    date: str | None = None
    reviewer: str | None = None
    rating: float | None = None
    text: str
    language: str | None = None
    aspects: dict[str, Any] | None = None
    sentiment: float | None = None


class ReviewsResponse(BaseModel):
    results: list[ReviewItem]
    total: int
    page: int
    page_size: int


# ── Batch compare ─────────────────────────────────────────────────────────────
class CompareMatrix(BaseModel):
    listings: list[ListingDetail]
    verdict: str | None = None   # AI-generated verdict — deferred to agent phase
