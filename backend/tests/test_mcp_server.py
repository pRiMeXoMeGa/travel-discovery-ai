"""Tests for the WS2 MCP server (`app/mcp_server/*`).

Zero network, zero LLM quota: every test monkeypatches the service layer
(`app.services.*`) the same way `test_services.py` does — no real
Postgres/Qdrant/Redis/Gemini call ever happens.

`pytest.importorskip("fastmcp")` guards the whole file: `fastmcp` lives only
in `backend/requirements.txt`, not `requirements-dev.txt` (see that file's
own docstring — real CI, `.github/workflows/ci.yml`, installs only
requirements-dev.txt). Under that lighter environment this file is SKIPPED,
not failed — matching how `app/main.py` degrades to "no /mcp route" rather
than crashing when fastmcp is unavailable (see its module-level try/except).
Under the full Docker image (`backend/requirements.txt`, fastmcp pinned),
these tests run for real against the real fastmcp `Tool`/`FastMCP` classes.

Tool-calling technique: `@mcp.tool` (bare decorator) returns a `FunctionTool`
whose `.fn` attribute IS the original async function unchanged — verified
against fastmcp==2.14.7 source (`fastmcp/tools/tool.py::ParsedFunction.
from_function` returns `cls(fn=fn, ...)`, the original callable, not a
wrapper). So `server.compare_listings.fn(listing_ids=[...])` calls the exact
function this module defines, without going through fastmcp's JSON-RPC/
schema-validation plumbing — appropriate for unit-testing tool logic, not
the wire protocol.
"""
from __future__ import annotations

from datetime import date
from typing import get_args

import pytest

fastmcp = pytest.importorskip("fastmcp")

from app.mcp_server import server as mcp_server  # noqa: E402
from app.mcp_server.auth import BearerAuthMiddleware, RateLimitMiddleware  # noqa: E402
from app.schemas import Citation, ListingDetail  # noqa: E402

EXPECTED_TOOL_NAMES = {
    "search_listings",
    "get_listing_detail",
    "check_availability",
    "compare_listings",
    "synthesize_reviews",
    "plan_itinerary",
}


def _detail(**overrides) -> ListingDetail:
    base = dict(
        id="l1", name="Cozy Loft", type="Entire home/apt", city="Amsterdam",
        neighbourhood="Jordaan", lat=52.37, lng=4.89, base_price=100.0, beds=2,
        amenities=["wifi", "kitchen"], photos=["photo1.jpg"], host={"name": "Ana"},
        rating=4.5, review_count=10,
    )
    base.update(overrides)
    return ListingDetail(**base)


# ── Registration ──────────────────────────────────────────────────────────

async def test_all_six_tools_registered_with_expected_names():
    tools = await mcp_server.mcp.get_tools()
    assert set(tools) == EXPECTED_TOOL_NAMES


async def test_tool_docstrings_are_non_empty():
    """Docstrings ARE the MCP schema a calling model reads — a blank one is
    a real regression, not a style nit."""
    tools = await mcp_server.mcp.get_tools()
    for name, tool in tools.items():
        assert tool.description, f"{name} has no description"


def test_city_enum_values_are_exactly_title_case():
    assert get_args(mcp_server.City) == ("Amsterdam", "Lisbon", "Los Angeles")


# ── compare_listings: must use the verdict-free path (WS2 requirement) ──────

async def test_compare_listings_tool_does_not_hit_verdict_path(monkeypatch):
    async def _fake_compare_listings(listing_ids):
        return [_detail(id=lid) for lid in listing_ids]

    async def _boom_with_verdict(*args, **kwargs):
        raise AssertionError(
            "MCP compare_listings tool must call the verdict-free "
            "services.listings.compare_listings, never compare_listings_with_verdict"
        )

    monkeypatch.setattr(mcp_server.listings_service, "compare_listings", _fake_compare_listings)
    monkeypatch.setattr(
        mcp_server.listings_service, "compare_listings_with_verdict", _boom_with_verdict
    )

    result = await mcp_server.compare_listings.fn(listing_ids=["l1", "l2"])
    assert result["count"] == 2
    assert [item["id"] for item in result["listings"]] == ["l1", "l2"]
    # No verdict field at all — this is the matrix-only shape, not CompareMatrix.
    assert "verdict" not in result


async def test_compare_listings_tool_reports_partial_match(monkeypatch):
    async def _fake_compare_listings(listing_ids):
        return [_detail(id="l1")]  # l2 silently not found, matching the service contract

    monkeypatch.setattr(mcp_server.listings_service, "compare_listings", _fake_compare_listings)

    result = await mcp_server.compare_listings.fn(listing_ids=["l1", "l2"])
    assert result["count"] == 1


# ── synthesize_reviews: abstention contract ──────────────────────────────────

async def test_synthesize_reviews_surfaces_abstained(monkeypatch):
    async def _fake_synthesize(listing_ids, focus=None):
        assert listing_ids == ["l1"]
        return {
            "text": "No guest reviews are available for this listing.",
            "citations": [],
            "abstained": True,
            "reason": "no_reviews",
        }

    monkeypatch.setattr(mcp_server.reviews_service, "synthesize_reviews", _fake_synthesize)

    result = await mcp_server.synthesize_reviews.fn(listing_id="l1")
    assert result["abstained"] is True
    assert result["reason"] == "no_reviews"
    assert result["citations"] == []


async def test_synthesize_reviews_serializes_real_citations(monkeypatch):
    async def _fake_synthesize(listing_ids, focus=None):
        return {
            "text": "Guests loved the location [r1].",
            "citations": [Citation(kind="review", id="r1", snippet="Great spot")],
            "abstained": False,
            "reason": "",
        }

    monkeypatch.setattr(mcp_server.reviews_service, "synthesize_reviews", _fake_synthesize)

    result = await mcp_server.synthesize_reviews.fn(listing_id="l1", focus="location")
    assert result["abstained"] is False
    assert result["citations"] == [{"kind": "review", "id": "r1", "snippet": "Great spot"}]


# ── search_listings: builds SearchFilters correctly, no crash on bad dates ──

async def test_search_listings_builds_filters_and_returns_json(monkeypatch):
    captured = {}

    async def _fake_search_listings(filters):
        captured["filters"] = filters
        from app.schemas import SearchResponse
        return SearchResponse(results=[], total=0, page=1, page_size=filters.page_size)

    monkeypatch.setattr(mcp_server.listings_service, "search_listings", _fake_search_listings)

    result = await mcp_server.search_listings.fn(
        city="Lisbon", price_max=150, room_type="Private room", limit=5,
    )
    assert result == {"results": [], "total": 0, "page": 1, "page_size": 5}
    filters = captured["filters"]
    assert filters.city == "Lisbon"
    assert filters.price_max == 150
    assert filters.property_types == ["Private room"]
    assert filters.page_size == 5


async def test_search_listings_invalid_date_returns_structured_error():
    result = await mcp_server.search_listings.fn(city="Amsterdam", check_in="not-a-date")
    assert result["error"] == "invalid_date"


async def test_search_listings_downstream_failure_degrades_to_structured_error(monkeypatch):
    async def _boom(filters):
        raise RuntimeError("db unreachable")

    monkeypatch.setattr(mcp_server.listings_service, "search_listings", _boom)

    result = await mcp_server.search_listings.fn(city="Amsterdam")
    assert result["error"] == "internal_error"


# ── get_listing_detail: not-found is a structured error, not an exception ───

async def test_get_listing_detail_not_found(monkeypatch):
    async def _fake_get_listing_detail(listing_id):
        return None

    monkeypatch.setattr(mcp_server.listings_service, "get_listing_detail", _fake_get_listing_detail)

    result = await mcp_server.get_listing_detail.fn(listing_id="missing")
    assert result == {"error": "not_found", "listing_id": "missing"}


async def test_get_listing_detail_found_returns_json_dict(monkeypatch):
    async def _fake_get_listing_detail(listing_id):
        return _detail(id=listing_id)

    monkeypatch.setattr(mcp_server.listings_service, "get_listing_detail", _fake_get_listing_detail)

    result = await mcp_server.get_listing_detail.fn(listing_id="l1")
    assert result["id"] == "l1"
    assert result["city"] == "Amsterdam"


# ── check_availability: fetches base_price via get_listing_detail first ─────

async def test_check_availability_not_found(monkeypatch):
    async def _fake_get_listing_detail(listing_id):
        return None

    monkeypatch.setattr(mcp_server.listings_service, "get_listing_detail", _fake_get_listing_detail)

    result = await mcp_server.check_availability.fn(
        listing_id="missing", check_in="2026-03-01", check_out="2026-03-03",
    )
    assert result == {"error": "not_found", "listing_id": "missing"}


async def test_check_availability_uses_real_base_price(monkeypatch):
    async def _fake_get_listing_detail(listing_id):
        return _detail(id=listing_id, base_price=123.0)

    captured = {}

    def _fake_range(listing_id, check_in, check_out, base_price):
        captured["args"] = (listing_id, check_in, check_out, base_price)
        return {"available": True, "total_price": 246.0, "nights": []}

    monkeypatch.setattr(mcp_server.listings_service, "get_listing_detail", _fake_get_listing_detail)
    monkeypatch.setattr(mcp_server.availability_service, "check_availability_range", _fake_range)

    result = await mcp_server.check_availability.fn(
        listing_id="l1", check_in="2026-03-01", check_out="2026-03-03",
    )
    assert result["total_price"] == 246.0
    assert captured["args"] == ("l1", date(2026, 3, 1), date(2026, 3, 3), 123.0)


async def test_check_availability_invalid_date_returns_structured_error(monkeypatch):
    async def _fake_get_listing_detail(listing_id):
        return _detail(id=listing_id)

    monkeypatch.setattr(mcp_server.listings_service, "get_listing_detail", _fake_get_listing_detail)

    result = await mcp_server.check_availability.fn(
        listing_id="l1", check_in="2026-03-01", check_out="not-a-date",
    )
    assert result["error"] == "invalid_date"


# ── plan_itinerary: builds a StructuredQuery correctly ───────────────────────

async def test_plan_itinerary_builds_structured_query(monkeypatch):
    captured = {}

    async def _fake_plan_itinerary(sq, candidates_per_stay=5, step=None):
        captured["sq"] = sq
        return {"city": sq.city, "stays": [], "notes": []}

    monkeypatch.setattr(mcp_server.planning_service, "plan_itinerary", _fake_plan_itinerary)

    result = await mcp_server.plan_itinerary.fn(
        city="Lisbon", check_in="2026-04-01", check_out="2026-04-05",
        party_size=3, budget_total=900.0, preferences="quiet, central",
    )
    assert result["city"] == "Lisbon"
    sq = captured["sq"]
    assert sq.check_in == date(2026, 4, 1)
    assert sq.check_out == date(2026, 4, 5)
    assert sq.party_size == 3
    assert sq.budget_total == 900.0
    assert sq.soft_preferences == ["quiet, central"]


async def test_plan_itinerary_invalid_date_returns_structured_error():
    result = await mcp_server.plan_itinerary.fn(
        city="Lisbon", check_in="bad", check_out="2026-04-05",
    )
    assert result["error"] == "invalid_date"


# ── Auth middleware: fail-closed + bearer check ──────────────────────────────

class _RecordingReceiveSend:
    """Minimal ASGI send() recorder + a receive() yielding one empty body."""

    def __init__(self, body: bytes = b""):
        self.sent: list[dict] = []
        self._body = body
        self._delivered = False

    async def send(self, message: dict) -> None:
        self.sent.append(message)

    async def receive(self) -> dict:
        if self._delivered:
            return {"type": "http.disconnect"}
        self._delivered = True
        return {"type": "http.request", "body": self._body, "more_body": False}

    @property
    def status(self) -> int | None:
        starts = [m for m in self.sent if m["type"] == "http.response.start"]
        return starts[0]["status"] if starts else None


def _scope(headers: dict[str, str]) -> dict:
    return {
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }


async def test_unauthenticated_request_is_rejected_with_401():
    async def _inner(scope, receive, send):
        raise AssertionError("inner app must not run when the bearer token is missing")

    mw = BearerAuthMiddleware(_inner, api_key="correct-key")
    io = _RecordingReceiveSend()

    await mw(_scope({}), io.receive, io.send)
    assert io.status == 401


async def test_wrong_bearer_token_is_rejected_with_401():
    async def _inner(scope, receive, send):
        raise AssertionError("inner app must not run when the bearer token is wrong")

    mw = BearerAuthMiddleware(_inner, api_key="correct-key")
    io = _RecordingReceiveSend()

    await mw(_scope({"Authorization": "Bearer wrong-key"}), io.receive, io.send)
    assert io.status == 401


async def test_correct_bearer_token_is_forwarded_to_inner_app():
    called = {}

    async def _inner(scope, receive, send):
        called["yes"] = True

    mw = BearerAuthMiddleware(_inner, api_key="correct-key")
    io = _RecordingReceiveSend()

    await mw(_scope({"Authorization": "Bearer correct-key"}), io.receive, io.send)
    assert called.get("yes") is True


async def test_unconfigured_api_key_fails_closed_not_open():
    """No MCP_API_KEY set must refuse ALL requests (503), never serve
    unauthenticated — two of six tools spend Gemini quota."""
    async def _inner(scope, receive, send):
        raise AssertionError("an unconfigured key must never let requests through")

    mw = BearerAuthMiddleware(_inner, api_key=None)
    io = _RecordingReceiveSend()

    await mw(_scope({"Authorization": "Bearer anything"}), io.receive, io.send)
    assert io.status == 503


# ── Rate-limit middleware: caps only the two LLM-backed tools ───────────────

def _tool_call_body(tool_name: str) -> bytes:
    import json
    return json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool_name, "arguments": {}}}
    ).encode()


async def test_rate_limit_caps_llm_tool_after_configured_rpm():
    calls = {"n": 0}

    async def _inner(scope, receive, send):
        calls["n"] += 1

    mw = RateLimitMiddleware(_inner, rpm=1)
    body = _tool_call_body("plan_itinerary")

    io1 = _RecordingReceiveSend(body)
    await mw(_scope({}), io1.receive, io1.send)
    assert calls["n"] == 1

    io2 = _RecordingReceiveSend(body)
    await mw(_scope({}), io2.receive, io2.send)
    assert io2.status == 429
    assert calls["n"] == 1  # inner app not called a second time


async def test_rate_limit_does_not_apply_to_zero_llm_tools():
    calls = {"n": 0}

    async def _inner(scope, receive, send):
        calls["n"] += 1

    mw = RateLimitMiddleware(_inner, rpm=1)
    body = _tool_call_body("search_listings")

    for _ in range(5):
        io = _RecordingReceiveSend(body)
        await mw(_scope({}), io.receive, io.send)

    assert calls["n"] == 5


async def test_rate_limit_forwards_original_body_unchanged():
    """The middleware buffers+replays the body — must not corrupt it."""
    received_bodies = []

    async def _inner(scope, receive, send):
        msg = await receive()
        received_bodies.append(msg["body"])

    mw = RateLimitMiddleware(_inner, rpm=10)
    body = _tool_call_body("get_listing_detail")

    io = _RecordingReceiveSend(body)
    await mw(_scope({}), io.receive, io.send)
    assert received_bodies == [body]
