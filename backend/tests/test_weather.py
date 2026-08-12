"""Unit tests for the weather MCP client (WS2 inbound) — `app/weather.py` and
its hook into `app/agents/itinerary.py::plan_itinerary`.

The MCP client is mocked entirely: no real `fastmcp` network call, no real
Redis, no LLM call. `app.weather` itself already degrades to a no-op when
`fastmcp` is not importable (see its module docstring) — which is exactly the
dev/CI situation here, since `fastmcp` is a heavy dep listed only in
`backend/requirements.txt`, not `requirements-dev.txt` (matches how
`tests/conftest.py` treats qdrant/redis/asyncpg/fastembed: this module just
handles its own absence instead of needing a conftest stub). Every test below
therefore monkeypatches `weather._get_client` directly with a fake object
implementing the same `async with client:` + `await client.call_tool(...)`
shape `fastmcp.Client` exposes, so these tests exercise the SAME code path
regardless of whether the real package happens to be installed.

Relies on `backend/tests/conftest.py` for dummy provider keys and the
no-network guard (autouse, applies here too). Async coroutines are driven
with `asyncio.run`, matching `test_itinerary.py`'s convention (no
pytest-asyncio dependency).
"""
import asyncio
import json
import sys
import types
from datetime import date
from pathlib import Path
from typing import Any

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app import llm, weather  # noqa: E402
from app.agents import itinerary  # noqa: E402
from app.observability import AgentStep, RequestTrace  # noqa: E402
from app.schemas import ListingCard, StructuredQuery  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# ── Fakes ────────────────────────────────────────────────────────────────────
def payload(
    temps=(18.0, 24.0),
    description="Mainly clear",
    rain_days: tuple[str, ...] = (),
) -> str:
    """A response shaped like the REAL weather MCP server's.

    That server does not return a human summary — it returns an instruction to
    an LLM ("Please analyze the following JSON…"), then field documentation,
    then hourly rows. `app.weather` parses those rows and formats the note
    itself, deterministically, so a fixture of plain prose would test a code
    path that cannot occur in production. Verified against the running
    `dog830228/mcp_weather_server` image.
    """
    rows = [
        {"time": f"2026-08-14T{h:02d}:00", "temperature_c": t,
         "weather_description": description, "precipitation_probability_percent": 0}
        for h, t in enumerate(temps)
    ]
    rows += [
        {"time": f"{day}T12:00", "temperature_c": temps[0],
         "weather_description": description, "precipitation_probability_percent": 80}
        for day in rain_days
    ]
    return (
        "Please analyze the following JSON weather forecast information.\n"
        "=== FIELD DESCRIPTIONS ===\n- temperature_c: air temperature\n\n"
        + json.dumps({"city": "Lisbon", "weather_data": rows})
    )


class _FakeClient:
    """Stand-in for `fastmcp.Client` — `async with` + `call_tool()` only."""

    # Sentinel so "not provided" (use a realistic payload) stays distinct from
    # an explicit text=None, which several tests use to mean "server returned
    # no usable content". Defaulting on `is None` collapses the two and makes
    # the malformed-response tests silently exercise the happy path.
    _UNSET = object()

    def __init__(self, text: Any = _UNSET, exc: Exception | None = None,
                 sleep: float = 0.0, is_error: bool = False):
        self.text = payload() if text is self._UNSET else text
        self.exc = exc
        self.sleep = sleep
        self.is_error = is_error
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        if self.sleep:
            await asyncio.sleep(self.sleep)
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def call_tool(self, name: str, arguments: dict, **kwargs):
        self.calls.append((name, dict(arguments)))
        if self.exc is not None:
            raise self.exc
        content = [types.SimpleNamespace(text=self.text)] if self.text is not None else []
        return types.SimpleNamespace(data=None, content=content, is_error=self.is_error)


def _wire_cache(monkeypatch, module) -> dict[str, Any]:
    """Real in-memory cache (dict-backed) so hit/miss behaviour is testable —
    the conftest.py redis stub always returns None from `.get()`, so it
    cannot express a real cache hit."""
    store: dict[str, Any] = {}

    async def fake_get(key):
        return store.get(key)

    async def fake_set(key, value, ttl=None):
        store[key] = value

    monkeypatch.setattr(module, "cache_get", fake_get)
    monkeypatch.setattr(module, "cache_set", fake_set)
    return store


def _no_cache(monkeypatch, module) -> None:
    """Always-miss cache, so a test can ignore caching entirely."""

    async def fake_get(key):
        return None

    async def fake_set(key, value, ttl=None):
        return None

    monkeypatch.setattr(module, "cache_get", fake_get)
    monkeypatch.setattr(module, "cache_set", fake_set)


# ═════════════════════════════════════════════════════════════════════════════
# app.weather.get_forecast_note — unit level
# ═════════════════════════════════════════════════════════════════════════════
def test_successful_forecast_returns_a_note(monkeypatch):
    _no_cache(monkeypatch, weather)
    fake = _FakeClient(text=payload(rain_days=("2026-08-15",)))
    monkeypatch.setattr(weather, "_get_client", lambda: fake)
    step = AgentStep("weather_mcp", "start")

    note = _run(weather.get_forecast_note("Lisbon", date(2026, 9, 14), date(2026, 9, 16), step=step))

    assert note is not None
    assert "Lisbon" in note
    # Deterministic summary built from the payload rows, not passed through.
    assert "18-24°C" in note and "rain likely on 2026-08-15" in note
    assert len(fake.calls) == 1
    # Asserted against the module constant, which is verified against the
    # real server image — hardcoding the name here let a typo
    # ("get_weather_by_datetime_range") pass tests while failing live.
    assert fake.calls[0][0] == weather._TOOL_NAME
    assert fake.calls[0][1] == {
        "city": "Lisbon", "start_date": "2026-09-14", "end_date": "2026-09-16",
    }
    assert step.status == "done"


def test_no_city_skips_without_calling_client(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(weather, "_get_client", lambda: fake)
    step = AgentStep("weather_mcp", "start")

    note = _run(weather.get_forecast_note(None, date(2026, 9, 14), date(2026, 9, 16), step=step))

    assert note is None
    assert fake.calls == []
    assert step.status == "skipped"


def test_timeout_returns_none_and_raises_nothing(monkeypatch):
    """The important one: a hung server must degrade to no note, not an
    exception — a third-party outage must never break a trip plan."""
    _no_cache(monkeypatch, weather)
    monkeypatch.setattr(weather, "_TIMEOUT_SECONDS", 0.05)
    fake = _FakeClient(text="should never be seen", sleep=5.0)
    monkeypatch.setattr(weather, "_get_client", lambda: fake)
    step = AgentStep("weather_mcp", "start")

    note = _run(weather.get_forecast_note("Lisbon", date(2026, 9, 14), date(2026, 9, 16), step=step))

    assert note is None
    assert step.status == "error"


def test_unreachable_server_error_degrades_silently(monkeypatch):
    _no_cache(monkeypatch, weather)
    fake = _FakeClient(exc=ConnectionRefusedError("no route to host"))
    monkeypatch.setattr(weather, "_get_client", lambda: fake)
    step = AgentStep("weather_mcp", "start")

    note = _run(weather.get_forecast_note("Lisbon", date(2026, 9, 14), date(2026, 9, 16), step=step))

    assert note is None
    assert step.status == "error"


def test_unset_server_client_none_degrades_silently(monkeypatch):
    """fastmcp missing / no URL configured — `_get_client()` returns None."""
    _no_cache(monkeypatch, weather)
    monkeypatch.setattr(weather, "_get_client", lambda: None)
    step = AgentStep("weather_mcp", "start")

    note = _run(weather.get_forecast_note("Lisbon", date(2026, 9, 14), date(2026, 9, 16), step=step))

    assert note is None
    assert step.status == "skipped"


def test_malformed_response_with_no_text_yields_no_note(monkeypatch):
    _no_cache(monkeypatch, weather)
    fake = _FakeClient(text=None)  # no content blocks, no .data
    monkeypatch.setattr(weather, "_get_client", lambda: fake)

    note = _run(weather.get_forecast_note("Lisbon", date(2026, 9, 14), date(2026, 9, 16)))

    assert note is None


def test_cache_consulted_before_a_second_call(monkeypatch):
    _wire_cache(monkeypatch, weather)
    fake = _FakeClient(text=payload())
    monkeypatch.setattr(weather, "_get_client", lambda: fake)

    note1 = _run(weather.get_forecast_note("Lisbon", date(2026, 9, 14), date(2026, 9, 16)))
    note2 = _run(weather.get_forecast_note("Lisbon", date(2026, 9, 14), date(2026, 9, 16)))

    assert note1 == note2
    assert len(fake.calls) == 1  # second call was a cache hit


def test_failure_is_not_cached_so_the_next_call_retries(monkeypatch):
    """Deliberate: caching a failure for 6h would hide a weather server's
    recovery (e.g. mid-demo restart) for hours. Every failed attempt gets a
    fresh try next time, bounded to the 3s timeout either way."""
    _wire_cache(monkeypatch, weather)
    fake = _FakeClient(text=None)  # malformed: no usable text
    monkeypatch.setattr(weather, "_get_client", lambda: fake)

    note1 = _run(weather.get_forecast_note("Lisbon", date(2026, 9, 14), date(2026, 9, 16)))
    note2 = _run(weather.get_forecast_note("Lisbon", date(2026, 9, 14), date(2026, 9, 16)))

    assert note1 is None and note2 is None
    assert len(fake.calls) == 2  # NOT cached — both calls actually hit the client


def test_cache_key_distinguishes_city_and_range(monkeypatch):
    _wire_cache(monkeypatch, weather)
    fake = _FakeClient(text=payload())
    monkeypatch.setattr(weather, "_get_client", lambda: fake)

    _run(weather.get_forecast_note("Lisbon", date(2026, 9, 14), date(2026, 9, 16)))
    _run(weather.get_forecast_note("Amsterdam", date(2026, 9, 14), date(2026, 9, 16)))
    _run(weather.get_forecast_note("Lisbon", date(2026, 10, 1), date(2026, 10, 3)))

    assert len(fake.calls) == 3  # three distinct (city, range) keys, no false cache hits


# ═════════════════════════════════════════════════════════════════════════════
# app.agents.itinerary.plan_itinerary — integration with the weather hook
# ═════════════════════════════════════════════════════════════════════════════
def _card(listing_id: str, price: float = 100.0, rating: float = 4.5) -> ListingCard:
    return ListingCard(
        id=listing_id, name=f"Listing {listing_id}", type="Entire home/apt",
        city="Lisbon", neighbourhood="Santa Maria Maior", lat=38.71, lng=-9.13,
        price_per_night=price, rating=rating, review_count=10,
        key_amenities=["wifi"], photo=None,
    )


def _sq(**over) -> StructuredQuery:
    base = dict(city="Lisbon", check_in=date(2026, 9, 1), check_out=date(2026, 9, 3))
    base.update(over)
    return StructuredQuery(**base)


def _force_single_segment(monkeypatch):
    async def fake_complete_json_with_usage(prompt, schema, system):
        raise llm.LLMError("forced failure for test determinism")

    monkeypatch.setattr(itinerary.llm, "complete_json_with_usage", fake_complete_json_with_usage)


def _force_two_segments(monkeypatch, total_nights: int):
    async def fake_complete_json_with_usage(prompt, schema, system):
        payload = {
            "segments": [
                {"nights": 1, "theme": "mid-range", "budget_per_night": None, "hard_constraints": []},
                {"nights": max(1, total_nights - 1), "theme": "splurge",
                 "budget_per_night": None, "hard_constraints": []},
            ]
        }
        return payload, llm.Usage(input_tokens=10, output_tokens=5)

    monkeypatch.setattr(itinerary.llm, "complete_json_with_usage", fake_complete_json_with_usage)


def _always_available(monkeypatch):
    def fake_is_available_range(listing_id, check_in, check_out, base_price):
        nights = (check_out - check_in).days
        return True, round(base_price * nights, 2)

    monkeypatch.setattr(itinerary, "is_available_range", fake_is_available_range)


def _wire_retrieve(monkeypatch, default_cards):
    async def fake_retrieve(sq, limit=20, exclude=None):
        return [(card, f"rationale for {card.id}") for card in default_cards]

    monkeypatch.setattr(itinerary.retrieval, "retrieve", fake_retrieve)


def test_successful_forecast_produces_a_note_in_plan(monkeypatch):
    _force_single_segment(monkeypatch)
    _always_available(monkeypatch)
    _wire_retrieve(monkeypatch, [_card("a")])
    _no_cache(monkeypatch, weather)
    fake = _FakeClient(text=payload(rain_days=("2026-08-15",)))
    monkeypatch.setattr(weather, "_get_client", lambda: fake)

    plan = _run(itinerary.plan_itinerary(_sq()))

    assert len(fake.calls) == 1
    assert any("rain likely on 2026-08-15" in n for n in plan["notes"])


def test_timeout_yields_no_note_and_no_exception(monkeypatch):
    _force_single_segment(monkeypatch)
    _always_available(monkeypatch)
    _wire_retrieve(monkeypatch, [_card("a")])
    _no_cache(monkeypatch, weather)
    monkeypatch.setattr(weather, "_TIMEOUT_SECONDS", 0.05)
    fake = _FakeClient(text="should never be seen", sleep=5.0)
    monkeypatch.setattr(weather, "_get_client", lambda: fake)

    plan = _run(itinerary.plan_itinerary(_sq()))  # must not raise

    assert plan["stays"]  # the rest of the plan is unaffected
    assert plan["notes"] == []  # no dealbreaker/budget notes here either, so
    # an empty list proves no weather note was appended.


def test_unreachable_server_degrades_silently_in_plan(monkeypatch):
    _force_single_segment(monkeypatch)
    _always_available(monkeypatch)
    _wire_retrieve(monkeypatch, [_card("a")])
    _no_cache(monkeypatch, weather)
    monkeypatch.setattr(weather, "_get_client", lambda: None)

    plan = _run(itinerary.plan_itinerary(_sq()))

    assert plan["stays"]
    assert plan["notes"] == []


def test_exactly_one_weather_call_per_plan_not_per_stay(monkeypatch):
    _force_two_segments(monkeypatch, total_nights=4)
    _always_available(monkeypatch)
    _wire_retrieve(monkeypatch, [_card("a"), _card("b")])
    _no_cache(monkeypatch, weather)
    fake = _FakeClient(text=payload())
    monkeypatch.setattr(weather, "_get_client", lambda: fake)

    plan = _run(itinerary.plan_itinerary(_sq()))

    assert len(plan["stays"]) == 2  # two segments really did run
    assert len(fake.calls) == 1  # but only one weather call total


def test_weather_step_lands_on_trace_when_provided(monkeypatch):
    _force_single_segment(monkeypatch)
    _always_available(monkeypatch)
    _wire_retrieve(monkeypatch, [_card("a")])
    _no_cache(monkeypatch, weather)
    fake = _FakeClient(text=payload())
    monkeypatch.setattr(weather, "_get_client", lambda: fake)
    trace = RequestTrace(request_id="t1", query="q")

    _run(itinerary.plan_itinerary(_sq(), trace=trace))

    weather_steps = [s for s in trace.steps if s.agent == "weather_mcp"]
    assert len(weather_steps) == 1
    assert weather_steps[0].status == "done"


def test_no_trace_still_works_and_omits_no_functionality(monkeypatch):
    """`trace=None` (every pre-WS2 caller) must not change plan output —
    only whether the weather step is separately traced."""
    _force_single_segment(monkeypatch)
    _always_available(monkeypatch)
    _wire_retrieve(monkeypatch, [_card("a")])
    _no_cache(monkeypatch, weather)
    fake = _FakeClient(text=payload())
    monkeypatch.setattr(weather, "_get_client", lambda: fake)

    plan = _run(itinerary.plan_itinerary(_sq()))  # no trace kwarg at all

    assert any("Weather for your" in n for n in plan["notes"])
