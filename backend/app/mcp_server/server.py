"""MCP server — exposes the travel platform as tools other agents can call.

Mounted into the existing FastAPI app at /mcp rather than run as its own
process: one deploy, one cold start, one keep-warm ping, and the tools share
the existing asyncpg pool, Qdrant client and Redis cache.

Tools call `app/services/*` directly. They must NEVER call this app's own
HTTP endpoints — a self-request on a single-worker Render instance risks
deadlock and doubles latency for nothing.

Mounting + lifespan chaining lives in app/main.py, not here — see that
module's comment for why (`build_mcp_app()`'s ASGI app carries its own
lifespan that initialises fastmcp's session manager; skip chaining it into
FastAPI's lifespan and every request fails with an opaque "session not
initialised" error).

NOTE ON DOCSTRINGS: every tool docstring below becomes the tool description
in the MCP schema. It is the only thing a calling model sees when deciding
whether to invoke the tool — declare grounding guarantees, cost, and
abstention behaviour. Do not "tidy" them into human-facing prose.
"""
from __future__ import annotations

import logging
import time
from datetime import date
from typing import Annotated, Any, Literal, Optional

from fastmcp import FastMCP
from pydantic import Field

from ..schemas import SearchFilters, StructuredQuery
from ..services import availability as availability_service
from ..services import listings as listings_service
from ..services import planning as planning_service
from ..services import reviews as reviews_service

logger = logging.getLogger(__name__)

mcp = FastMCP(
    name="travel-discovery",
    instructions=(
        "Search and compare real short-stay rental listings across Amsterdam, "
        "Lisbon and Los Angeles, backed by 50,000 real listings and 200,000 real "
        "guest reviews. Prefer search_listings for browsing; it costs nothing. "
        "synthesize_reviews and plan_itinerary each perform an LLM call — use "
        "them when the user needs cited evidence or a multi-stay plan, not for "
        "browsing."
    ),
)

# MUST be title-case. This vocabulary is shared platform-wide: listings.city
# stores "Amsterdam" / "Lisbon" / "Los Angeles" in Postgres, and
# agents/retrieval.py's semantic path matches it against Qdrant via an exact
# MatchValue keyword filter — a lowercase "lisbon" there matches nothing and
# returns an empty result set with NO error. The Postgres-backed
# search_listings tool below happens to be case-insensitive
# (LOWER(city) = LOWER($1)), which would mask the bug in testing but not fix
# it for the rest of the app, so the enum is title-case here regardless.
City = Literal["Amsterdam", "Lisbon", "Los Angeles"]

# Exact Inside Airbnb values — anything else matches zero rows (property
# type is an equality filter, not a fuzzy match).
RoomType = Literal["Entire home/apt", "Private room", "Shared room", "Hotel room"]


# ── Zero-cost tools ───────────────────────────────────────────────────────────

@mcp.tool
async def search_listings(
    city: Annotated[City, Field(description="One of the three supported cities.")],
    price_min: Annotated[Optional[float], Field(
        description="Minimum price per night, in the listing's currency.")] = None,
    price_max: Annotated[Optional[float], Field(
        description="Maximum price per night, in the listing's currency.")] = None,
    min_rating: Annotated[Optional[float], Field(
        description="Minimum listing review score, 0-5. Listings with no "
                    "rating yet are excluded whenever this is set.")] = None,
    room_type: Annotated[Optional[RoomType], Field(
        description="Exact Inside Airbnb value: 'Entire home/apt', 'Private "
                    "room', 'Shared room', or 'Hotel room'.")] = None,
    amenities: Annotated[Optional[list[str]], Field(
        description="Required amenities — ALL must be present, e.g. "
                    "['wifi', 'kitchen']. Vocabulary: wifi, pool, kitchen, "
                    "parking, balcony, ac, gym, washer, pets_allowed, "
                    "hot_tub, bbq, workspace, beach_access, concierge, "
                    "breakfast_included, ev_charger, elevator, baby_cot.")] = None,
    adults: Annotated[int, Field(ge=1, le=16)] = 1,
    children: Annotated[int, Field(ge=0, le=16)] = 0,
    check_in: Annotated[Optional[str], Field(
        description="ISO date YYYY-MM-DD. When both dates are given, a "
                    "listing unavailable for any night in the range is "
                    "excluded from results.")] = None,
    check_out: Annotated[Optional[str], Field(description="ISO date YYYY-MM-DD.")] = None,
    sort: Annotated[Literal["price_asc", "rating", "popularity"], Field(
        description="popularity sorts by review_count DESC.")] = "popularity",
    limit: Annotated[int, Field(ge=1, le=24)] = 10,
) -> dict[str, Any]:
    """Filtered search over real Postgres listing records — the workhorse
    tool; prefer this for browsing and narrowing before spending an LLM call
    on synthesize_reviews or plan_itinerary.

    Cost: no LLM call.

    Grounding: every field returned is a real Inside Airbnb attribute —
    nothing here is generated. This is traditional filter/sort search (SQL
    over Postgres), not semantic/vector search: there is no free-text
    relevance ranking, only hard constraints — city, price range,
    min_rating, room_type, amenities (all must be present), and guest
    capacity (adults+children, checked against `beds` as a capacity proxy).
    A result will never violate one of the filters you passed.

    Returns {"results": [...], "total": int}. `total` counts matches BEFORE
    the check_in/check_out availability filter is applied (an accepted
    upstream limitation at this dataset scale), so it can run slightly
    higher than len(results) when a date range is given. An empty list means
    nothing matched the constraints — relax one rather than retrying the
    same query.
    """
    try:
        parsed_check_in = date.fromisoformat(check_in) if check_in else None
        parsed_check_out = date.fromisoformat(check_out) if check_out else None
    except ValueError as exc:
        return {"error": "invalid_date", "detail": str(exc)}

    filters = SearchFilters(
        city=city,
        check_in=parsed_check_in,
        check_out=parsed_check_out,
        adults=adults,
        children=children,
        price_min=price_min,
        price_max=price_max,
        min_rating=min_rating,
        property_types=[room_type] if room_type else [],
        amenities=amenities or [],
        sort=sort,
        page=1,
        page_size=limit,
    )

    t0 = time.perf_counter()
    try:
        response = await listings_service.search_listings(filters)
    except Exception as exc:  # noqa: BLE001 - tools degrade, never crash the server
        logger.warning("mcp search_listings failed: %s", exc)
        return {"error": "internal_error", "detail": str(exc)}
    logger.info(
        "mcp tool=search_listings city=%s elapsed_ms=%.1f results=%d",
        city, (time.perf_counter() - t0) * 1000, len(response.results),
    )
    return response.model_dump(mode="json")


@mcp.tool
async def get_listing_detail(
    listing_id: Annotated[str, Field(description="ID from search_listings results.")],
) -> dict[str, Any]:
    """Full detail for one listing: gallery, amenities, aspect scores, price
    breakdown, and a 30-day availability preview.

    Cost: no LLM call.

    Aspect scores (cleanliness, noise, location, host) are precomputed
    offline from real review text by a keyword heuristic that is
    English-biased — treat them as indicative, not authoritative. For
    citable evidence a guest could actually read, call synthesize_reviews
    instead.

    Returns {"error": "not_found", "listing_id": ...} if the ID does not
    exist, rather than raising.
    """
    try:
        detail = await listings_service.get_listing_detail(listing_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("mcp get_listing_detail failed (id=%s): %s", listing_id, exc)
        return {"error": "internal_error", "detail": str(exc)}

    if detail is None:
        return {"error": "not_found", "listing_id": listing_id}
    return detail.model_dump(mode="json")


@mcp.tool
async def check_availability(
    listing_id: Annotated[str, Field(description="ID from search_listings or get_listing_detail.")],
    check_in: Annotated[str, Field(description="ISO date, YYYY-MM-DD.")],
    check_out: Annotated[str, Field(description="ISO date, YYYY-MM-DD (exclusive — checkout morning).")],
) -> dict[str, Any]:
    """Per-night availability and pricing for a date range.

    Cost: no LLM call.

    IMPORTANT: availability and per-night price are computed deterministically
    from the listing's real base price by a synthetic, stable calendar
    function — not read from a live booking system. Do not present this to a
    user as a live Airbnb calendar.

    Returns {"available": bool, "total_price": float, "nights": [{"date",
    "available", "price"}, ...]}. `available` is true only when every night
    in the range is free; `total_price` is 0.0 whenever any night is
    unavailable. A non-positive range (check_out <= check_in) returns an
    empty, unavailable result rather than an error.

    Returns {"error": "not_found", "listing_id": ...} if the listing does
    not exist.
    """
    try:
        detail = await listings_service.get_listing_detail(listing_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("mcp check_availability failed (id=%s): %s", listing_id, exc)
        return {"error": "internal_error", "detail": str(exc)}

    if detail is None:
        return {"error": "not_found", "listing_id": listing_id}

    try:
        parsed_in = date.fromisoformat(check_in)
        parsed_out = date.fromisoformat(check_out)
    except ValueError as exc:
        return {"error": "invalid_date", "detail": str(exc)}

    # check_availability_range is pure CPU (no I/O) — no await needed.
    return availability_service.check_availability_range(
        listing_id, parsed_in, parsed_out, detail.base_price
    )


@mcp.tool
async def compare_listings(
    listing_ids: Annotated[list[str], Field(
        min_length=2, max_length=4,
        description="2-4 listing IDs from search_listings.")],
) -> dict[str, Any]:
    """Side-by-side factual matrix for 2-4 listings: price, amenities,
    rating, review count, precomputed summary.

    Cost: no LLM call — this is deliberately the verdict-free path
    (`services.listings.compare_listings`), NOT the `POST /api/batch/compare`
    HTTP endpoint, which always spends up to 5 LLM calls (per-listing review
    synthesis fanned out via asyncio.gather, plus one verdict call). This
    tool never touches that budget.

    For a reasoned verdict, call synthesize_reviews on each shortlisted
    listing yourself and compare the grounded evidence — that keeps the
    judgement with the calling agent and the citations attached to the
    claims, instead of hiding both behind a generated verdict.

    Listing IDs that don't exist are silently skipped (partial match is
    acceptable) — check "count" against the number of IDs you requested.

    Returns {"listings": [...], "count": int}.
    """
    try:
        results = await listings_service.compare_listings(listing_ids)
    except Exception as exc:  # noqa: BLE001
        logger.warning("mcp compare_listings failed: %s", exc)
        return {"error": "internal_error", "detail": str(exc)}

    return {
        "listings": [item.model_dump(mode="json") for item in results],
        "count": len(results),
    }


# ── LLM-backed tools — rate limited (see mcp_server/auth.py) ────────────────

@mcp.tool
async def synthesize_reviews(
    listing_id: Annotated[str, Field(description="ID from search_listings or get_listing_detail.")],
    focus: Annotated[Optional[str], Field(
        description="What to weigh the evidence toward, e.g. 'noise at "
                    "night', 'wifi reliability', 'cleanliness'. Omit for a "
                    "balanced view.")] = None,
) -> dict[str, Any]:
    """Grounded synthesis of a property's real guest reviews, with citations.

    Cost: ONE LLM call. Narrow with search_listings first; call this only on
    a shortlist, never while browsing.

    Grounding guarantee: reviews are ranked from a real 200,000-review corpus
    by Postgres full-text search scoped to this listing, and every claim in
    `text` carries a `[r#]` marker that maps to a real row in `citations` —
    each citation includes the actual review text, so any claim is
    independently verifiable, never model-invented.

    Abstention: when no reviews match, returns `{"abstained": true, "reason":
    "no_reviews" | "no_listing_specified" | "no_evidence", "citations": []}`
    instead of inventing a summary. Treat that as genuine absence of
    evidence, not a signal to retry the same call.

    Coverage caveat: the corpus averages ~4 reviews per listing, so a
    synthesis may rest on very few rows — check len(citations) before
    treating it as strong evidence.
    """
    t0 = time.perf_counter()
    try:
        result = await reviews_service.synthesize_reviews([listing_id], focus=focus)
    except Exception as exc:  # noqa: BLE001
        logger.warning("mcp synthesize_reviews failed (id=%s): %s", listing_id, exc)
        return {"error": "internal_error", "detail": str(exc)}

    logger.info(
        "mcp tool=synthesize_reviews id=%s abstained=%s elapsed_ms=%.1f",
        listing_id, result["abstained"], (time.perf_counter() - t0) * 1000,
    )
    return {
        "text": result["text"],
        "citations": [c.model_dump(mode="json") for c in result["citations"]],
        "abstained": result["abstained"],
        "reason": result["reason"],
    }


@mcp.tool
async def plan_itinerary(
    city: City,
    check_in: Annotated[str, Field(description="ISO date, YYYY-MM-DD.")],
    check_out: Annotated[str, Field(description="ISO date, YYYY-MM-DD.")],
    party_size: Annotated[int, Field(ge=1, le=16)] = 2,
    budget_total: Annotated[Optional[float], Field(
        description="Total accommodation budget for the whole stay, in the "
                    "city's currency.")] = None,
    preferences: Annotated[Optional[str], Field(
        description="Free text, e.g. 'first nights central, then somewhere "
                    "quiet'. Folded in as a soft preference the planner "
                    "tries to honour — not a hard constraint.")] = None,
) -> dict[str, Any]:
    """Multi-stay plan with real properties, real per-stay costs, and
    swap-out alternatives.

    Cost: ONE LLM call, regardless of how many stay segments the plan ends
    up with.

    Division of labour: the model decides only the segment structure (how
    many stays, their themes, night counts). Property selection and every
    cost figure are computed deterministically from real listing prices —
    totals are arithmetic over real data, not model estimates.

    Returns {"city", "total_nights", "total_cost", "currency_note",
    "within_budget", "stays": [...], "notes": [...]}. Read `notes` before
    presenting the plan — over-budget and thin-availability warnings land
    there, not as an exception.
    """
    try:
        parsed_in = date.fromisoformat(check_in)
        parsed_out = date.fromisoformat(check_out)
    except ValueError as exc:
        return {"error": "invalid_date", "detail": str(exc)}

    sq = StructuredQuery(
        city=city,
        check_in=parsed_in,
        check_out=parsed_out,
        party_size=party_size,
        budget_total=budget_total,
        soft_preferences=[preferences] if preferences else [],
    )

    t0 = time.perf_counter()
    try:
        plan = await planning_service.plan_itinerary(sq)
    except Exception as exc:  # noqa: BLE001
        # Log AND return the exception type, not just str(exc): several
        # qdrant/httpx transport errors stringify to "", which made this
        # surface as `mcp plan_itinerary failed (city=Lisbon): ` server-side
        # and `{"error": "internal_error", "detail": ""}` to the caller —
        # a failure with no evidence at either end.
        logger.warning(
            "mcp plan_itinerary failed (city=%s): %s: %s",
            city, type(exc).__name__, exc,
        )
        return {
            "error": "internal_error",
            "detail": f"{type(exc).__name__}: {exc}".rstrip(": "),
        }

    logger.info(
        "mcp tool=plan_itinerary city=%s elapsed_ms=%.1f",
        city, (time.perf_counter() - t0) * 1000,
    )
    return plan


# ── App construction ─────────────────────────────────────────────────────────

def build_mcp_app():
    """ASGI app for the MCP server. Mounted at /mcp in app/main.py, which
    also chains `.lifespan` into FastAPI's own lifespan — required or
    fastmcp's session manager is never initialised and every request fails
    with an opaque session error.
    """
    return mcp.http_app(path="/")
