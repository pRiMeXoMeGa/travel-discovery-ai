"""MCP server — exposes the travel platform as tools other agents can call.

Mounted into the existing FastAPI app at /mcp rather than run as its own process:
one deploy, one cold start, one keep-warm ping, and the tools share the existing
asyncpg pool, Qdrant client and Redis cache.

Tools call app/services/* directly. They must NEVER call this app's own HTTP
endpoints — a self-request on a single-worker Render instance risks deadlock and
doubles latency for nothing.

Mounting (in app/main.py)
-------------------------
FastMCP's ASGI app carries its own lifespan that initialises the session manager.
If it is not chained into FastAPI's lifespan, every MCP request fails with an
opaque "session not initialised" error. Chain them:

    from contextlib import asynccontextmanager
    from .mcp_server.server import build_mcp_app, BearerAuthMiddleware

    mcp_app = build_mcp_app()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with mcp_app.lifespan(app):      # MCP session manager
            await get_pool()                   # existing: warm the asyncpg pool
            init_memory()                      # WS1
            yield
            await close_pool()                 # existing teardown, in this order
            await close_qdrant()
            await close_redis()

    app = FastAPI(lifespan=lifespan)
    app.mount("/mcp", BearerAuthMiddleware(mcp_app, settings.mcp_api_key))

Those are the real function names in app/main.py today (get_pool / close_pool /
close_qdrant / close_redis) — there is no startup_pools/shutdown_pools pair.

`settings.mcp_api_key` does not exist yet: add `mcp_api_key: str | None = None`
to app/config.py::Settings, declare MCP_API_KEY with `sync: false` in render.yaml,
and add it to .env.example.

Auth is ASGI middleware on purpose: transport-agnostic, and immune to fastmcp's
auth API shifting between releases.

`fastmcp` is in no requirements file yet. `@mcp.tool` (bare, no parentheses) and
`mcp.http_app(path="/")` are FastMCP 2.x APIs — pin the version and re-check both
against the release you install; this decorator spelling changed in 1.x.

NOTE ON DOCSTRINGS: every tool docstring below becomes the tool description in
the MCP schema. It is the only thing a calling model sees when deciding whether
to invoke the tool. Write for that reader — declare grounding guarantees, cost,
and abstention behaviour. Do not "tidy" them into human-facing prose.
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated, Any, Literal, Optional

from fastmcp import FastMCP
from pydantic import Field

logger = logging.getLogger(__name__)

mcp = FastMCP(
    name="travel-discovery",
    instructions=(
        "Search and compare real short-stay rental listings across Amsterdam, "
        "Lisbon and Los Angeles, backed by 50,000 real listings and 200,000 real "
        "guest reviews. Prefer search_listings for browsing; it costs nothing. "
        "synthesize_reviews and plan_itinerary each perform an LLM call — use "
        "them when the user needs evidence or a multi-stay plan, not for browsing."
    ),
)

# MUST be title-case — these values are matched verbatim against the Qdrant
# payload. listings.city stores "Amsterdam" / "Lisbon" / "Los Angeles", and
# agents/retrieval.py::_build_qdrant_filter uses MatchValue(value=sq.city), an
# exact keyword match. Lowercase "lisbon" matches nothing and the tool returns
# an empty result set with no error. (The SQL path in routers/search.py is
# case-insensitive via LOWER(city) = LOWER($1), which masks this in testing —
# the semantic path is where it bites.)
City = Literal["Amsterdam", "Lisbon", "Los Angeles"]


# ── Zero-cost tools ───────────────────────────────────────────────────────────

@mcp.tool
async def search_listings(
    city: Annotated[City, Field(description="One of the three supported cities.")],
    query: Annotated[Optional[str], Field(
        description="Free-text description of what the guest wants, e.g. 'quiet "
                    "studio near the canals with a workspace'. Matched "
                    "semantically against listing text.")] = None,
    price_max: Annotated[Optional[float], Field(
        description="Maximum price per night, in the listing's currency.")] = None,
    room_type: Annotated[Optional[str], Field(
        description="Exact Inside Airbnb value: 'Entire home/apt', 'Private room', "
                    "'Shared room', or 'Hotel room'.")] = None,
    min_rating: Annotated[Optional[float], Field(
        description="Minimum listing review score, 0-5.")] = None,
    limit: Annotated[int, Field(ge=1, le=20)] = 10,
) -> dict[str, Any]:
    """Search real listings by semantic similarity fused with hard filters.

    Cost: no LLM call. Cheap — prefer this for browsing and narrowing.

    Grounding: every field returned comes from the real Inside Airbnb record.
    The `rationale` on each result is built deterministically from real fields,
    never generated, so it cannot describe an attribute the listing lacks.

    Filters are hard constraints applied in the vector store, not ranking hints:
    a result will never violate price_max or room_type.

    Returns {"results": [...], "total": int}. An empty list means nothing matched
    the constraints — relax one rather than retrying the same query.

    IMPLEMENTATION NOTE (remove before shipping):
    `min_rating` is NOT currently enforceable on the semantic path.
    _build_qdrant_filter supports city / base_price / beds / type / amenities /
    neighbourhood only — there is no rating condition — and `rating` is absent
    from _LISTINGS_PAYLOAD_INDEXES (city, type, neighbourhood, amenities,
    base_price, beds). Adding a rating filter therefore also requires a payload
    index plus a re-snapshot, or Qdrant Cloud strict mode returns 400. Until
    then, either drop min_rating from this tool's signature or have
    services/listings.py apply it in Python after hydration and say so here.
    """
    from ..services import listings

    return await listings.search_listings(
        city=city, query=query, price_max=price_max,
        room_type=room_type, min_rating=min_rating, limit=limit,
    )


@mcp.tool
async def get_listing_detail(
    listing_id: Annotated[str, Field(description="ID from search_listings.")],
) -> dict[str, Any]:
    """Full detail for one listing: gallery, amenities, aspect scores, price breakdown.

    Cost: no LLM call.

    Aspect scores (cleanliness, noise, location, host) are computed offline from
    real review text by a keyword heuristic that is English-biased — treat them as
    indicative. For evidence a guest can read, call synthesize_reviews instead.
    """
    from ..services import listings

    return await listings.get_listing(listing_id)


@mcp.tool
async def check_availability(
    listing_id: str,
    check_in: Annotated[str, Field(description="ISO date, YYYY-MM-DD.")],
    check_out: Annotated[str, Field(description="ISO date, YYYY-MM-DD.")],
) -> dict[str, Any]:
    """Per-night availability and pricing for a date range.

    Cost: no LLM call.

    IMPORTANT: availability is computed deterministically from the listing's real
    base price, not read from a live Airbnb calendar. Per-night prices derive from
    real base prices; the availability pattern itself is synthetic and stable.
    Do not present it to a user as a live booking calendar.
    """
    from ..services import availability

    return await availability.check_availability(listing_id, check_in, check_out)


@mcp.tool
async def compare_listings(
    listing_ids: Annotated[list[str], Field(min_length=2, max_length=4)],
    check_in: Optional[str] = None,
    check_out: Optional[str] = None,
) -> dict[str, Any]:
    """Side-by-side matrix for 2-4 listings: price, amenities, rating, availability.

    Cost: no LLM call — this returns the factual matrix only.

    For a reasoned verdict, call synthesize_reviews on each listing and compare the
    grounded evidence yourself. That keeps the judgement with you and the citations
    with the data.

    IMPLEMENTATION NOTE (remove before shipping):
    The existing POST /api/batch/compare ALWAYS builds the AI verdict — it runs
    review_intel.synthesize() per listing via asyncio.gather plus one verdict
    call, so up to 5 LLM calls. services/listings.py::compare_listings must
    expose a verdict-free path for this tool, otherwise "no LLM call" is false
    and a browsing agent silently burns quota. `check_in`/`check_out` are also
    new: CompareRequest currently accepts listing_ids only.
    """
    from ..services import listings

    return await listings.compare_listings(listing_ids, check_in, check_out)


# ── LLM-backed tools — rate limit these ───────────────────────────────────────

@mcp.tool
async def synthesize_reviews(
    listing_id: str,
    focus: Annotated[Optional[str], Field(
        description="What to weigh the evidence toward, e.g. 'noise at night', "
                    "'wifi reliability', 'cleanliness'. Omit for a balanced view."
    )] = None,
) -> dict[str, Any]:
    """Grounded synthesis of a property's real guest reviews, with citations.

    Cost: ONE LLM call. Do not call it while browsing — narrow with
    search_listings first, then call this on the shortlist.

    Grounding guarantee: reviews are ranked from the real corpus by Postgres
    full-text search scoped to this listing, and every claim in the summary
    carries a [r#] marker referencing a row in the returned `citations` array.
    Each citation contains the real review text, so any claim can be verified.

    Abstention: when no reviews match the focus, returns
    {"abstained": true, "reason": ...} rather than inventing a summary. Treat
    that as genuine absence of evidence, not as a failure to retry.

    Coverage caveat: retrieval is keyword-based, aspect coverage is sparse for
    non-English reviews, and the corpus averages ~4 reviews per listing, so a
    synthesis may rest on very few rows. Check the length of `citations`.

    IMPLEMENTATION NOTE (remove before shipping):
    The abstention contract does not exist yet. agents/review_intel.py::synthesize
    returns tuple[str, list[Citation]] and, when no reviews are found, returns a
    plain English sentence with an empty citation list — no flag. services/
    reviews.py must add {"abstained": bool, "reason": str} rather than assume it.
    Also fix the snippet sampler first: its no-focus fallback orders by `rating`,
    a column that is ALWAYS NULL in this corpus, so the "balanced top/bottom"
    selection is a no-op (order by `sentiment`, which is populated). See
    FINDINGS.md 2.2 and 2.6.
    """
    from ..services import reviews

    return await reviews.synthesize_reviews(listing_id, focus=focus)


@mcp.tool
async def plan_itinerary(
    city: City,
    check_in: str,
    check_out: str,
    party_size: Annotated[int, Field(ge=1, le=16)] = 2,
    budget_total: Annotated[Optional[float], Field(
        description="Total accommodation budget for the whole stay.")] = None,
    preferences: Annotated[Optional[str], Field(
        description="Free text, e.g. 'first nights central, then somewhere quiet'."
    )] = None,
) -> dict[str, Any]:
    """Multi-stay plan with real properties, real costs and swap-out alternatives.

    Cost: ONE LLM call.

    Division of labour: the model decides only the segment structure (how to split
    the trip). Property selection and all costing are deterministic, computed from
    real prices — so totals are arithmetic, not estimates.

    Returns stays with check-in/out, chosen property, stay cost, ranked
    alternatives, a budget check, and `notes` carrying any constraint that could
    not be satisfied. Read `notes` before presenting the plan; it is where
    over-budget and thin-availability warnings appear.
    """
    from ..services import planning

    return await planning.plan_itinerary(
        city=city, check_in=check_in, check_out=check_out,
        party_size=party_size, budget_total=budget_total, preferences=preferences,
    )


# ── App construction + auth ───────────────────────────────────────────────────

def build_mcp_app():
    """ASGI app for the MCP server. Mount at /mcp; chain its lifespan."""
    return mcp.http_app(path="/")


class BearerAuthMiddleware:
    """Reject unauthenticated MCP calls.

    Plain ASGI rather than a fastmcp auth hook: transport-agnostic, and it will
    not break when fastmcp's auth API changes between releases.

    Leave api_key empty to disable (local dev only — never in production; two of
    the six tools spend Gemini quota).
    """

    def __init__(self, app, api_key: Optional[str]):
        self.app = app
        self.api_key = api_key

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.api_key:
            return await self.app(scope, receive, send)

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}

        # Accept "Bearer <key>" case-insensitively, or a bare x-api-key header.
        raw = headers.get("authorization", "").strip()
        if raw[:7].lower() == "bearer ":
            raw = raw[7:].strip()
        supplied = raw or headers.get("x-api-key", "").strip()

        # Constant-time compare — a plain != leaks key material through timing.
        if not hmac.compare_digest(supplied, self.api_key):
            logger.warning("MCP request rejected: bad or missing key")
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"application/json")],
            })
            await send({
                "type": "http.response.body",
                "body": b'{"error":"unauthorized"}',
            })
            return

        await self.app(scope, receive, send)
