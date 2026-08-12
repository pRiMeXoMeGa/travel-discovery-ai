"""Unit tests for the WS0-C service layer (`app/services/*`).

Relies on `conftest.py`'s heavy-dependency stubs (qdrant_client, redis,
asyncpg, fastembed) and its network-block fixture — this file is NOT
self-contained the way `test_review_intel.py`/`test_llm_usage.py` are,
because `pyproject.toml` already puts `backend/` on `sys.path` via the
`pythonpath` ini option, so `import app...` just works under pytest.

What is covered:
  * `services.listings`: the pure SQL/param builder, row->card mapping
    (available/unavailable), constraint-filter merging (the replacement for
    routers/agents.py's old `_parse_constraints` import), search caching
    (hit short-circuits the DB; miss populates it), the no-cache/no-
    availability-filter fallback path used by orchestrator's graceful
    degradation, listing detail (found/not-found), and — the WS2
    requirement — that `compare_listings` (matrix only) makes ZERO LLM
    calls while `compare_listings_with_verdict` does call the LLM once.
  * `services.reviews`: the abstention contract (`abstained`/`reason`) for
    both no-evidence paths plus the has-citations path.
  * `services.availability`: the date-range -> per-night helper stays
    internally consistent with `is_available_range`/`availability_window`.
  * `services.planning`: thin delegation to `agents.itinerary.plan_itinerary`.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.availability import availability_window, is_available_range
from app.schemas import SearchFilters, StructuredQuery
from app.services import availability as availability_service
from app.services import listings as listings_service
from app.services import planning as planning_service
from app.services import reviews as reviews_service


# ── Fake asyncpg pool/connection ─────────────────────────────────────────────

class _FakeConnection:
    def __init__(self, *, fetch_rows=None, fetchval_result=None, fetchrow_result=None):
        self._fetch_rows = fetch_rows if fetch_rows is not None else []
        self._fetchval_result = fetchval_result
        self._fetchrow_result = fetchrow_result
        self.fetch_calls: list[tuple] = []
        self.fetchval_calls: list[tuple] = []
        self.fetchrow_calls: list[tuple] = []

    async def fetch(self, sql, *params):
        self.fetch_calls.append((sql, params))
        return self._fetch_rows

    async def fetchval(self, sql, *params):
        self.fetchval_calls.append((sql, params))
        return self._fetchval_result

    async def fetchrow(self, sql, *params):
        self.fetchrow_calls.append((sql, params))
        return self._fetchrow_result


class _FakeAcquireCtx:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConnection:
        return self._conn

    async def __aexit__(self, *exc_info) -> bool:
        return False


class _FakePool:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    def acquire(self) -> _FakeAcquireCtx:
        return _FakeAcquireCtx(self._conn)


def _pool_getter(conn: _FakeConnection):
    async def _get_pool():
        return _FakePool(conn)
    return _get_pool


def _search_row(
    id="l1", name="Cozy Loft", type="Entire home/apt", city="Amsterdam",
    neighbourhood="Jordaan", lat=52.37, lng=4.89, base_price=100.0, beds=2,
    amenities=None, photos=None, rating=4.5, review_count=10,
) -> dict:
    return {
        "id": id, "name": name, "type": type, "city": city,
        "neighbourhood": neighbourhood, "lat": lat, "lng": lng,
        "base_price": base_price, "beds": beds,
        "amenities": amenities if amenities is not None else ["wifi", "kitchen"],
        "photos": photos if photos is not None else ["photo1.jpg"],
        "rating": rating, "review_count": review_count,
    }


def _detail_row(**overrides) -> dict:
    row = {
        "id": "l1", "name": "Cozy Loft", "type": "Entire home/apt",
        "city": "Amsterdam", "neighbourhood": "Jordaan", "lat": 52.37,
        "lng": 4.89, "base_price": 100.0, "beds": 2,
        "amenities": ["wifi", "kitchen"], "photos": ["photo1.jpg"],
        "host": {"name": "Ana"}, "rating": 4.5, "review_count": 10,
        "neighbourhood_price_pct": 0.5, "summary": "A lovely stay.",
        "aspect_avg": {"cleanliness": 0.8},
    }
    row.update(overrides)
    return row


# ── services.listings: build_search_query (pure) ────────────────────────────

def test_build_search_query_no_filters_has_no_where_clause():
    filters = SearchFilters()
    count_sql, count_params, rows_sql, rows_params = listings_service.build_search_query(filters)
    assert "WHERE" not in count_sql
    assert count_params == []
    assert "ORDER BY review_count DESC" in rows_sql


def test_build_search_query_parameterizes_city_never_interpolates_it():
    filters = SearchFilters(city="Amsterdam; DROP TABLE listings;--")
    count_sql, count_params, rows_sql, rows_params = listings_service.build_search_query(filters)
    assert "DROP TABLE" not in count_sql
    assert "DROP TABLE" not in rows_sql
    assert "LOWER(city) = LOWER($1)" in count_sql
    assert count_params == ["Amsterdam; DROP TABLE listings;--"]


def test_build_search_query_distance_sort_appends_lat_lng_only_to_rows_params():
    filters = SearchFilters(sort="distance", near_lat=52.3, near_lng=4.9)
    count_sql, count_params, rows_sql, rows_params = listings_service.build_search_query(filters)
    # lat/lng are ORDER BY params — must not leak into the count query.
    assert 52.3 not in count_params
    assert 52.3 in rows_params
    assert "ASIN(SQRT(" in rows_sql


# ── services.listings: row_to_card ───────────────────────────────────────────

def test_row_to_card_available_range(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        listings_service, "is_available_range", lambda *a, **kw: (True, 250.0)
    )
    card, ok = listings_service.row_to_card(
        _search_row(), date(2026, 1, 1), date(2026, 1, 3), None, None
    )
    assert ok is True
    assert card.total_for_stay == 250.0
    assert card.key_amenities[:2] == ["wifi", "kitchen"]


def test_row_to_card_unavailable_range_returns_not_ok(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        listings_service, "is_available_range", lambda *a, **kw: (False, 0.0)
    )
    card, ok = listings_service.row_to_card(
        _search_row(), date(2026, 1, 1), date(2026, 1, 3), None, None
    )
    assert ok is False
    assert card is None


def test_row_to_card_no_dates_skips_availability_entirely(monkeypatch: pytest.MonkeyPatch):
    def _boom(*a, **kw):
        raise AssertionError("is_available_range must not be called with no date range")
    monkeypatch.setattr(listings_service, "is_available_range", _boom)
    card, ok = listings_service.row_to_card(_search_row(), None, None, None, None)
    assert ok is True
    assert card.total_for_stay is None


def test_row_to_card_distance_computed_when_near_point_given():
    card, ok = listings_service.row_to_card(_search_row(), None, None, 52.37, 4.89)
    assert ok is True
    assert card.distance_km == 0.0


# ── services.listings: apply_constraint_filters ─────────────────────────────

def test_apply_constraint_filters_merges_amenities_and_type():
    sq = StructuredQuery(
        city="Lisbon",
        hard_constraints=["wifi", "apartment"],
    )
    base_filters = SearchFilters(city="Lisbon", price_max=200)
    merged = listings_service.apply_constraint_filters(sq, base_filters)
    assert "wifi" in merged.amenities
    assert merged.property_types == ["Entire home/apt"]
    # Fields not touched by constraint-parsing are preserved.
    assert merged.city == "Lisbon"
    assert merged.price_max == 200


def test_apply_constraint_filters_no_constraints_leaves_lists_empty():
    sq = StructuredQuery(city="Lisbon")
    base_filters = SearchFilters(city="Lisbon")
    merged = listings_service.apply_constraint_filters(sq, base_filters)
    assert merged.amenities == []
    assert merged.property_types == []


# ── services.listings: search_listings caching ───────────────────────────────

@pytest.mark.asyncio
async def test_search_listings_cache_hit_never_touches_the_db(monkeypatch: pytest.MonkeyPatch):
    cached_payload = {
        "results": [], "total": 0, "page": 1, "page_size": 24,
    }

    async def _cache_get(key):
        return cached_payload

    async def _boom_get_pool():
        raise AssertionError("get_pool must not be called on a cache hit")

    monkeypatch.setattr(listings_service, "cache_get", _cache_get)
    monkeypatch.setattr(listings_service, "get_pool", _boom_get_pool)

    response = await listings_service.search_listings(SearchFilters())
    assert response.total == 0
    assert response.results == []


@pytest.mark.asyncio
async def test_search_listings_cache_miss_queries_db_and_writes_cache(
    monkeypatch: pytest.MonkeyPatch,
):
    conn = _FakeConnection(fetch_rows=[_search_row()], fetchval_result=1)
    set_calls = []

    async def _cache_get(key):
        return None

    async def _cache_set(key, value, ttl=None):
        set_calls.append((key, value, ttl))

    monkeypatch.setattr(listings_service, "cache_get", _cache_get)
    monkeypatch.setattr(listings_service, "cache_set", _cache_set)
    monkeypatch.setattr(listings_service, "get_pool", _pool_getter(conn))

    response = await listings_service.search_listings(SearchFilters())
    assert response.total == 1
    assert len(response.results) == 1
    assert response.results[0].id == "l1"
    assert len(set_calls) == 1
    assert set_calls[0][2] == 120  # ttl


# ── services.listings: fallback_search_candidates (orchestrator degrade path) ─

@pytest.mark.asyncio
async def test_fallback_search_candidates_caps_at_limit_and_skips_cache(
    monkeypatch: pytest.MonkeyPatch,
):
    rows = [_search_row(id=f"l{i}") for i in range(15)]
    conn = _FakeConnection(fetch_rows=rows)

    async def _boom_cache_get(key):
        raise AssertionError("fallback path must not read the cache")

    monkeypatch.setattr(listings_service, "cache_get", _boom_cache_get)
    monkeypatch.setattr(listings_service, "get_pool", _pool_getter(conn))

    cards = await listings_service.fallback_search_candidates(SearchFilters(), limit=10)
    assert len(cards) == 10
    assert cards[0].id == "l0"


# ── services.listings: get_listing_detail ────────────────────────────────────

@pytest.mark.asyncio
async def test_get_listing_detail_not_found_returns_none(monkeypatch: pytest.MonkeyPatch):
    conn = _FakeConnection(fetchrow_result=None)

    async def _cache_get(key):
        return None

    monkeypatch.setattr(listings_service, "cache_get", _cache_get)
    monkeypatch.setattr(listings_service, "get_pool", _pool_getter(conn))

    detail = await listings_service.get_listing_detail("missing")
    assert detail is None


@pytest.mark.asyncio
async def test_get_listing_detail_found_populates_cache(monkeypatch: pytest.MonkeyPatch):
    conn = _FakeConnection(fetchrow_result=_detail_row())
    set_calls = []

    async def _cache_get(key):
        return None

    async def _cache_set(key, value, ttl=None):
        set_calls.append(key)

    monkeypatch.setattr(listings_service, "cache_get", _cache_get)
    monkeypatch.setattr(listings_service, "cache_set", _cache_set)
    monkeypatch.setattr(listings_service, "get_pool", _pool_getter(conn))

    detail = await listings_service.get_listing_detail("l1")
    assert detail is not None
    assert detail.id == "l1"
    assert len(detail.availability_window) == 30
    assert set_calls == ["listing:l1"]


# ── services.listings: compare — verdict-free vs verdict paths (WS2 req) ────

@pytest.mark.asyncio
async def test_compare_listings_makes_zero_llm_calls(monkeypatch: pytest.MonkeyPatch):
    conn = _FakeConnection(fetch_rows=[_detail_row(id="l1"), _detail_row(id="l2")])

    async def _boom_complete_text(*a, **kw):
        raise AssertionError("compare_listings (verdict-free) must not call the LLM")

    monkeypatch.setattr(listings_service, "get_pool", _pool_getter(conn))
    monkeypatch.setattr(listings_service.llm, "complete_text", _boom_complete_text)

    out = await listings_service.compare_listings(["l1", "l2"])
    assert [d.id for d in out] == ["l1", "l2"]


@pytest.mark.asyncio
async def test_compare_listings_with_verdict_calls_llm_once(monkeypatch: pytest.MonkeyPatch):
    conn = _FakeConnection(fetch_rows=[_detail_row(id="l1"), _detail_row(id="l2")])

    async def _cache_get(key):
        return None

    async def _cache_set(key, value, ttl=None):
        pass

    async def _fake_synthesize_reviews(listing_ids, focus=None, step=None):
        return {"text": "guests loved it", "citations": [], "abstained": True, "reason": "no_reviews"}

    calls = []

    async def _fake_complete_text(prompt, system):
        calls.append(prompt)
        return "Property A is the better value."

    monkeypatch.setattr(listings_service, "get_pool", _pool_getter(conn))
    monkeypatch.setattr(listings_service, "cache_get", _cache_get)
    monkeypatch.setattr(listings_service, "cache_set", _cache_set)
    monkeypatch.setattr(
        listings_service.reviews_service, "synthesize_reviews", _fake_synthesize_reviews
    )
    monkeypatch.setattr(listings_service.llm, "complete_text", _fake_complete_text)

    matrix = await listings_service.compare_listings_with_verdict(["l1", "l2"])
    assert matrix.verdict == "Property A is the better value."
    assert len(calls) == 1
    assert len(matrix.listings) == 2


@pytest.mark.asyncio
async def test_compare_listings_with_verdict_single_listing_skips_llm(
    monkeypatch: pytest.MonkeyPatch,
):
    conn = _FakeConnection(fetch_rows=[_detail_row(id="l1")])

    async def _boom_complete_text(*a, **kw):
        raise AssertionError("a single listing has nothing to compare — no verdict call")

    monkeypatch.setattr(listings_service, "get_pool", _pool_getter(conn))
    monkeypatch.setattr(listings_service.llm, "complete_text", _boom_complete_text)

    matrix = await listings_service.compare_listings_with_verdict(["l1"])
    assert matrix.verdict is None
    assert len(matrix.listings) == 1


# ── services.reviews: abstention contract ────────────────────────────────────

@pytest.mark.asyncio
async def test_synthesize_reviews_abstains_when_no_listing_specified():
    result = await reviews_service.synthesize_reviews([])
    assert result["abstained"] is True
    assert result["reason"] == "no_listing_specified"
    assert result["citations"] == []


@pytest.mark.asyncio
async def test_synthesize_reviews_abstains_when_no_reviews_found():
    # conftest's asyncpg stub returns [] from every `.fetch()`, so
    # `_retrieve_review_snippets` finds nothing for any listing_id here —
    # no monkeypatching needed to exercise the real no-evidence path.
    result = await reviews_service.synthesize_reviews(["some-listing"])
    assert result["abstained"] is True
    assert result["reason"] == "no_reviews"
    assert result["citations"] == []


@pytest.mark.asyncio
async def test_synthesize_reviews_no_abstention_when_citations_present(
    monkeypatch: pytest.MonkeyPatch,
):
    from app.schemas import Citation

    async def _fake_synthesize(listing_ids, focus=None, step=None):
        return "Guests loved the location [r1].", [Citation(kind="review", id="r1", snippet="Great spot")]

    monkeypatch.setattr(reviews_service.review_intel, "synthesize", _fake_synthesize)

    result = await reviews_service.synthesize_reviews(["l1"], focus="location")
    assert result["abstained"] is False
    assert result["reason"] == ""
    assert len(result["citations"]) == 1
    assert result["text"].startswith("Guests loved")


# ── services.availability: date-range -> per-night helper ──────────────────

def test_check_availability_range_matches_is_available_range_and_window():
    listing_id = "l1"
    check_in = date(2026, 3, 1)
    check_out = check_in + timedelta(days=5)
    base_price = 120.0

    result = availability_service.check_availability_range(
        listing_id, check_in, check_out, base_price
    )
    expected_available, expected_total = is_available_range(
        listing_id, check_in, check_out, base_price
    )
    expected_nights = availability_window(listing_id, check_in, 5, base_price)

    assert result["available"] == expected_available
    assert result["total_price"] == expected_total
    assert result["nights"] == expected_nights
    assert len(result["nights"]) == 5


def test_check_availability_range_non_positive_range_is_empty_and_unavailable():
    listing_id = "l1"
    same_day = date(2026, 3, 1)
    result = availability_service.check_availability_range(listing_id, same_day, same_day, 100.0)
    assert result == {"available": False, "total_price": 0.0, "nights": []}

    result_reversed = availability_service.check_availability_range(
        listing_id, same_day, same_day - timedelta(days=1), 100.0
    )
    assert result_reversed["available"] is False
    assert result_reversed["nights"] == []


# ── services.planning: thin delegation ───────────────────────────────────────

@pytest.mark.asyncio
async def test_plan_itinerary_delegates_to_agents_itinerary(monkeypatch: pytest.MonkeyPatch):
    captured = {}

    async def _fake_plan_itinerary(sq, candidates_per_stay=5, step=None):
        captured["sq"] = sq
        captured["candidates_per_stay"] = candidates_per_stay
        return {"city": sq.city, "stays": []}

    monkeypatch.setattr(planning_service.itinerary, "plan_itinerary", _fake_plan_itinerary)

    sq = StructuredQuery(city="Lisbon")
    plan = await planning_service.plan_itinerary(sq, candidates_per_stay=3)
    assert plan == {"city": "Lisbon", "stays": []}
    assert captured["candidates_per_stay"] == 3
    assert captured["sq"] is sq
