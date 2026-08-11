"""Weather MCP client (WS2 inbound): the platform as an MCP *client*.

Consumes a third-party weather MCP server over streamable HTTP (default:
`dog830228/mcp_weather_server`, `get_weather_by_datetime_range(city,
start_date, end_date)` — no API key, backed by Open-Meteo). This is a genuine
external boundary, unlike wrapping our own DB in MCP: the server is a
separate process we do not control, so every call here is written assuming
it can be slow, down, or absent.

Hook point: `agents/itinerary.py::plan_itinerary`, once per plan (not per
stay), after the segment structure and trip dates are settled. The result is
folded into `plan["notes"]`, which the answer prompt already surfaces — so a
rain forecast can visibly change the concierge's recommendation.

Client discipline (non-negotiable, matches every other agent's contract in
`agents/orchestrator.py`):
  * ONE `fastmcp.Client`, created lazily on first use, reused for the life of
    the process — never one per request.
  * 3-second hard timeout around the whole connect+call, enforced with
    `asyncio.timeout` as a backstop *in addition to* fastmcp's own
    `timeout`/`init_timeout`, so a hang inside the transport layer itself
    cannot outlast it either.
  * Full graceful degradation: fastmcp not installed, no URL configured,
    connection refused, timeout, or a malformed/empty response all resolve
    to "skip the note, return None" — never an exception, never a broken
    trip plan. `get_forecast_note` is written so it CANNOT raise.
  * Cached by (city, date-range) in Redis, 6h TTL, via `app/cache.py` —
    successful lookups only. A failure/timeout/empty response is deliberately
    NOT cached: caching it for 6h would mean restarting a stopped weather
    server mid-demo wouldn't show recovery for hours, and a real outage is
    already bounded to a 3s cost per request, which does not need caching to
    stay cheap.
  * Zero Gemini calls — this is a tool call, not a completion.

Config: `WEATHER_MCP_URL` is read via `os.environ` directly (not
`app/config.py`), because `config.py` was being edited by another agent
building the MCP *server* side (WS2 outbound) at the same time this was
written. Whoever lands second should add `weather_mcp_url` to `Settings` and
swap the one `os.environ.get` call below for it — everything else is
unaffected.
"""
import asyncio
import json
import logging
import os
import re
from datetime import date

from .cache import cache_get, cache_set
from .observability import AgentStep

logger = logging.getLogger(__name__)

try:
    import fastmcp
except ImportError:  # pragma: no cover - not installed in dev/CI (see
    # backend/requirements-dev.txt's docstring: heavy/optional deps are
    # deliberately absent there and stubbed via sys.modules in conftest.py
    # for the others; fastmcp gets no stub, so this module degrades itself
    # instead — "not installed" and "installed but unreachable" collapse to
    # the exact same no-op code path below, which is the correct behaviour
    # either way.
    fastmcp = None  # type: ignore[assignment]

# Docker-compose service name + the image's documented default port/path
# (see docker-compose.yml's `weather-mcp` service, profile-gated under
# "tools"). Overridable; an unset/unreachable target degrades silently.
_WEATHER_MCP_URL_ENV = "WEATHER_MCP_URL"
_DEFAULT_URL = "http://weather-mcp:8080/mcp"
_TIMEOUT_SECONDS = 3.0
_CACHE_TTL_SECONDS = 6 * 3600
# Verified against the running `dog830228/mcp_weather_server` image via
# `list_tools()` — the name is camelCase after the underscore prefix, NOT
# `get_weather_by_datetime_range`. Getting it wrong does not raise: fastmcp
# returns a CallToolResult whose text is "Unknown tool: ...", which the old
# code happily formatted into a user-facing note.
_TOOL_NAME = "get_weather_byDateTimeRange"
_NOTE_MAX_CHARS = 280

_client: "fastmcp.Client | None" = None  # type: ignore[name-defined]


def _get_client():
    """Lazily create and reuse the single fastmcp Client for this process.

    Returns None (never raises) when fastmcp is unavailable or unconfigured
    — both are just flavours of "no weather integration this run".
    """
    global _client
    if fastmcp is None:
        return None
    if _client is None:
        url = os.environ.get(_WEATHER_MCP_URL_ENV, _DEFAULT_URL)
        if not url:
            return None
        _client = fastmcp.Client(
            url, timeout=_TIMEOUT_SECONDS, init_timeout=_TIMEOUT_SECONDS
        )
    return _client


def _cache_key(city: str, check_in: date, check_out: date) -> str:
    return f"weather:{city.strip().lower()}:{check_in.isoformat()}:{check_out.isoformat()}"


# Open-Meteo's forecast horizon is roughly 14 days. Measured against the
# running server: +3d and +10d return data, +14d, +20d and +30d return none.
# A trip planned further out therefore gets NO weather note — correct
# behaviour, not a failure, and it degrades down the same silent path. Worth
# knowing because `plan_itinerary` defaults to starting ~14 days out when the
# query carries no dates, which puts the default demo query right on the edge:
# ask for dates within two weeks to see a note reliably.
_FORECAST_HORIZON_DAYS = 14


class WeatherToolError(RuntimeError):
    """The MCP server answered, but the tool call itself failed.

    Raised so a tool-level error joins the same degradation path as a timeout
    or an outage, rather than being mistaken for a forecast.
    """


def _extract_text(result) -> str | None:
    """Pull the forecast text out of a `CallToolResult`.

    `get_weather_by_datetime_range` returns a narrative summary, not a
    declared output schema, so `result.data` is normally None and the text
    lives in the first text content block — but both are checked so a server
    that DOES declare structured output still works.
    """
    data = getattr(result, "data", None)
    if isinstance(data, str) and data.strip():
        return data.strip()
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text and text.strip():
            return text.strip()
    return None


def _summarise_payload(text: str) -> str | None:
    """Build a short forecast summary from the tool's JSON payload.

    This tool does NOT return a human summary — it returns an instruction to an
    LLM ("Please analyze the following JSON weather forecast information…"),
    followed by field documentation, followed by hourly rows. Passing that
    through verbatim put a wall of schema docs into `plan["notes"]`.

    So we parse the rows and format the summary ourselves, deterministically:
    same principle as retrieval rationales and itinerary costs — a number the
    traveller sees is computed from real data, never generated. Zero LLM calls.

    Returns None if no usable payload is present, which sends the caller down
    the normal "no forecast" path.
    """
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None

    rows = payload.get("weather_data") or []
    temps = [r["temperature_c"] for r in rows if isinstance(r.get("temperature_c"), (int, float))]
    if not temps:
        return None

    parts = [f"{round(min(temps))}-{round(max(temps))}°C"]

    # Most common description across the window — a plain mode, no weighting.
    descriptions = [r.get("weather_description") for r in rows if r.get("weather_description")]
    if descriptions:
        parts.append(max(set(descriptions), key=descriptions.count).lower())

    # Wet days, by the server's own hourly probability. Named explicitly
    # because "rain on the 15th" is the bit that actually changes a plan.
    wet: list[str] = []
    for row in rows:
        prob = row.get("precipitation_probability_percent")
        stamp = str(row.get("time") or "")
        if isinstance(prob, (int, float)) and prob >= 50 and len(stamp) >= 10:
            day = stamp[:10]
            if day not in wet:
                wet.append(day)
    if wet:
        parts.append("rain likely on " + ", ".join(wet[:3]))

    return "; ".join(parts)


def _format_note(city: str, check_in: date, check_out: date, text: str) -> str | None:
    summary = _summarise_payload(text)
    if not summary:
        return None
    if check_in == check_out:
        date_range = check_in.isoformat()
    else:
        date_range = f"{check_in.isoformat()} to {check_out.isoformat()}"
    note = f"Weather for your {city} segment ({date_range}): {summary}."
    if len(note) > _NOTE_MAX_CHARS:
        note = note[: _NOTE_MAX_CHARS - 3].rstrip() + "..."
    return note


async def get_forecast_note(
    city: str | None,
    check_in: date,
    check_out: date,
    step: AgentStep | None = None,
) -> str | None:
    """Best-effort forecast note for one trip's overall date window.

    Never raises. Returns None — meaning "add nothing to plan['notes']" — on
    every failure mode: no city, fastmcp missing, no/unreachable server,
    timeout, or a response with no usable text. `step`, when given, is
    populated with a status the caller can add to a `RequestTrace` so the
    call is visible in the SSE trace as its own `weather_mcp` step.
    """
    if not city:
        if step is not None:
            step.status, step.detail = "skipped", "no city on the query"
        return None

    key = _cache_key(city, check_in, check_out)
    try:
        cached = await cache_get(key)
    except Exception as exc:  # noqa: BLE001 - cache.py raises; caller wraps it
        logger.warning("weather cache_get failed: %s", exc)
        cached = None
    if cached is not None:
        if step is not None:
            step.status = "done"
            step.data = {"cache": "hit", "city": city, "found": True}
        return cached

    client = _get_client()
    if client is None:
        # No network call was attempted, so nothing to cache — this branch
        # is a cheap process-local check (fastmcp missing / no URL), not a
        # server round-trip, so there is no cost to amortize.
        if step is not None:
            step.status, step.detail = "skipped", "weather MCP not configured/installed"
        return None

    note: str | None = None
    try:
        async with asyncio.timeout(_TIMEOUT_SECONDS):
            async with client:
                result = await client.call_tool(
                    _TOOL_NAME,
                    {
                        "city": city,
                        "start_date": check_in.isoformat(),
                        "end_date": check_out.isoformat(),
                    },
                )
        if getattr(result, "is_error", False):
            # A tool-level error is a normal CallToolResult, not an exception,
            # so without this check the error TEXT ("Unknown tool: ...",
            # "Invalid city", a stack trace) gets formatted into plan["notes"]
            # and surfaced to the traveller as though it were the forecast —
            # and then cached for six hours. Treat it as no forecast.
            raise WeatherToolError(_extract_text(result) or "tool reported an error")

        text = _extract_text(result)
        if text:
            note = _format_note(city, check_in, check_out, text)
        if step is not None:
            step.status = "done"
            step.data = {"cache": "miss", "city": city, "found": note is not None}
    except Exception as exc:  # noqa: BLE001 - a third-party outage must never
        # break a trip plan or the SSE stream (same contract every agent in
        # orchestrator.py already honours). Covers timeout, connection
        # refused, DNS failure, malformed MCP response, ToolError, etc.
        logger.warning(
            "weather MCP call failed/timed out (%s: %s) — degrading silently",
            type(exc).__name__, exc,
        )
        if step is not None:
            step.status, step.detail = "error", f"{type(exc).__name__}: {exc}"

    if note:
        try:
            await cache_set(key, note, ttl=_CACHE_TTL_SECONDS)
        except Exception as exc:  # noqa: BLE001
            logger.warning("weather cache_set failed: %s", exc)

    return note
