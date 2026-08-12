"""ASGI middleware guarding the mounted MCP app: bearer auth + a per-key RPM
cap on the two LLM-backed tools.

Both are plain ASGI middleware, not fastmcp auth/middleware hooks, on
purpose: they are transport-agnostic (work the same whether fastmcp is
speaking streamable-HTTP or something else tomorrow) and immune to
fastmcp's own auth API shifting between releases — the exact reasoning
`version2/WS2_MCP.md` gives for this design.

Composition (see app/main.py): BearerAuthMiddleware wraps RateLimitMiddleware
wraps the FastMCP ASGI app, in that order — auth runs first so a request
with a bad/missing key is rejected before it can consume any rate-limit
budget, and the rate limiter only ever sees requests already known to carry
the one configured key.
"""
from __future__ import annotations

import hmac
import json
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Body larger than this is never JSON-parsed for tool-name detection (a
# legitimate JSON-RPC tools/call envelope is a few hundred bytes; nothing
# this MCP server exposes needs a multi-hundred-KB argument payload). The
# full body is still buffered and replayed byte-for-byte regardless — this
# cap only skips the *parse*, never truncates what's forwarded downstream.
_MAX_PARSE_BYTES = 200_000

# Absolute cap on how much body this middleware will buffer in memory before
# giving up on rate-limit inspection and simply streaming the rest through
# unread. Generous relative to any real MCP tool call; exists so a client
# can't force unbounded memory growth by streaming a huge body at a `/mcp`
# POST.
_MAX_BUFFER_BYTES = 5_000_000


async def _send_json_error(send, status: int, error: str, detail: str) -> None:
    body = json.dumps({"error": error, "detail": detail}).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json")],
    })
    await send({"type": "http.response.body", "body": body})


def _extract_bearer(scope) -> str:
    """Pull the credential out of `Authorization: Bearer <key>` (case-
    insensitive scheme) or a bare `x-api-key` header. Returns "" if neither
    is present — callers compare that against the configured key, which is
    never empty when auth is actually enforced (see fail-closed check)."""
    headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
    raw = headers.get("authorization", "").strip()
    if raw[:7].lower() == "bearer ":
        raw = raw[7:].strip()
    return raw or headers.get("x-api-key", "").strip()


class BearerAuthMiddleware:
    """Reject unauthenticated MCP calls. FAILS CLOSED: an unconfigured
    `api_key` refuses every request (503) rather than serving the endpoint
    unauthenticated. Two of the six tools spend Gemini quota — a public,
    unauthenticated MCP server would let anyone burn it.
    """

    def __init__(self, app, api_key: Optional[str]):
        self.app = app
        self.api_key = api_key

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        if not self.api_key:
            logger.warning("MCP request rejected: MCP_API_KEY is not configured (fail closed)")
            await _send_json_error(
                send, 503, "mcp_not_configured",
                "MCP_API_KEY is not set on this server; refusing to serve.",
            )
            return

        supplied = _extract_bearer(scope)

        # Constant-time compare — a plain `!=` leaks key material through
        # response-timing side channels.
        if not supplied or not hmac.compare_digest(supplied, self.api_key):
            logger.warning("MCP request rejected: bad or missing bearer token")
            await _send_json_error(send, 401, "unauthorized", "missing or invalid bearer token")
            return

        await self.app(scope, receive, send)


class RateLimitMiddleware:
    """Per-key fixed-window RPM cap, applied only to the two LLM-backed
    tools (`synthesize_reviews`, `plan_itinerary`).

    Streamable-HTTP tool calls are a single JSON-RPC POST body:
    `{"method": "tools/call", "params": {"name": "<tool>", ...}}`. There is
    no fastmcp hook that exposes "which tool is this call for" at the ASGI
    layer, so this middleware buffers the request body once, sniffs
    `params.name` out of it, and replays the SAME bytes to the wrapped app
    via a synthetic `receive()` — the downstream app sees an identical
    stream to what the client sent. Any tool name other than the two capped
    ones (including tools/list, initialize, notifications, or a body that
    doesn't parse as JSON) passes through with no limiting at all.
    """

    _LIMITED_TOOLS = frozenset({"synthesize_reviews", "plan_itinerary"})
    _WINDOW_SECONDS = 60.0

    def __init__(self, app, rpm: int):
        self.app = app
        self.rpm = rpm
        # key -> (window_start_monotonic, count_in_window). Single-process,
        # in-memory — matches the rest of this app's "single-worker Render
        # instance" assumption (see mcp_server/server.py's module docstring).
        self._windows: dict[str, tuple[float, int]] = {}

    def _check_and_record(self, key: str) -> bool:
        """Return True if this call is allowed under `key`'s current window.

        No `await` between the read and the write below, so this is
        effectively atomic within one ASGI task despite not using a lock —
        the event loop cannot switch tasks mid-function without a yield
        point.
        """
        now = time.monotonic()
        window_start, count = self._windows.get(key, (now, 0))
        if now - window_start >= self._WINDOW_SECONDS:
            window_start, count = now, 0
        if count >= self.rpm:
            return False
        self._windows[key] = (window_start, count + 1)
        return True

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        body_chunks: list[bytes] = []
        total = 0
        more_body = True
        while more_body:
            message = await receive()
            chunk = message.get("body", b"")
            total += len(chunk)
            if total <= _MAX_BUFFER_BYTES:
                body_chunks.append(chunk)
            more_body = message.get("more_body", False)
        body = b"".join(body_chunks)

        replayed = False

        async def replay_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            # Delegate to the REAL receive once the buffered body is exhausted.
            #
            # Returning a synthetic {"type": "http.disconnect"} here instead is
            # wrong in a way that only shows up on streaming responses: the MCP
            # streamable-HTTP handler keeps the SSE response open and calls
            # receive() again purely to watch for the client going away. Handing
            # it an immediate disconnect makes it abandon the response — the
            # client gets 200, the headers and an mcp-session-id, and then the
            # body never arrives and the call hangs until it times out.
            # The real receive blocks until an actual client event, which is
            # exactly the semantics the handler expects.
            return await receive()

        tool_name = _extract_tool_name(body)
        if tool_name in self._LIMITED_TOOLS:
            key = _extract_bearer(scope) or "unknown"
            if not self._check_and_record(key):
                logger.warning("MCP rate limit hit: tool=%s key=%.8s...", tool_name, key)
                await _send_json_error(
                    send, 429, "rate_limited",
                    f"'{tool_name}' is capped at {self.rpm} calls/{int(self._WINDOW_SECONDS)}s per key.",
                )
                return

        await self.app(scope, replay_receive, send)


def _extract_tool_name(body: bytes) -> Optional[str]:
    """Best-effort JSON-RPC `params.name` sniff. Returns None on anything
    that isn't a parseable `tools/call` envelope — malformed/oversized
    bodies are simply not rate-limited, they still reach fastmcp untouched
    and fail (or succeed) on their own terms."""
    if not body or len(body) > _MAX_PARSE_BYTES:
        return None
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None

    candidates = payload if isinstance(payload, list) else [payload]
    for item in candidates:
        if not isinstance(item, dict) or item.get("method") != "tools/call":
            continue
        params = item.get("params")
        if isinstance(params, dict) and isinstance(params.get("name"), str):
            return params["name"]
    return None
