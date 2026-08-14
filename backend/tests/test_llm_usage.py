"""Tests for WS0-D: real streamed token usage in app.llm + its orchestrator wiring.

Self-contained by design — does NOT rely on backend/tests/conftest.py (owned by
a parallel workstream). Everything this file needs (sys.path so `app` is
importable, dummy provider API keys, and stand-in modules for the four heavy/
infra dependencies app.agents.orchestrator transitively imports — qdrant_client,
redis, asyncpg, fastembed) is set up right here, guarded so it's a harmless
no-op if conftest.py (or the real packages) already did it first.

No real network I/O ever happens: `httpx.AsyncClient` itself is swapped out
(not merely `.send()` patched) for a fake that serves canned SSE frames off an
in-memory queue, so these tests exercise app.llm's real frame-parsing / retry /
usage-accumulation logic against fabricated Gemini `streamGenerateContent` and
Anthropic `messages` stream payloads.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import types
from pathlib import Path

import pytest

# ── 0a) Make `app` importable as a top-level package ─────────────────────────
_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ── 0b) Dummy provider keys BEFORE app.config.settings is constructed ────────
os.environ.setdefault("GEMINI_API_KEY", "test-dummy-gemini-key-not-real")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-anthropic-key-not-real")


def _install_stub_module(name: str, **attrs) -> types.ModuleType:
    if name in sys.modules:
        mod = sys.modules[name]
    else:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def _ensure_heavy_stubs() -> None:
    """Stand-ins for qdrant_client/redis/asyncpg/fastembed so importing
    `app.agents.orchestrator` (which pulls in app.db/app.cache/app.vectorstore/
    app.embeddings transitively via app.agents.retrieval) doesn't require those
    real packages to be installed. No-op for any package already importable
    (e.g. if conftest.py's own stubs, or the real deps, are already present).
    """
    try:
        import qdrant_client  # noqa: F401
    except ImportError:
        class _StubAsyncQdrantClient:
            def __init__(self, *a, **kw) -> None:
                pass

            async def close(self) -> None:
                pass

        models_mod = _install_stub_module("qdrant_client.models")
        for cls_name in ("Filter", "Condition", "FieldCondition", "MatchValue", "Range"):
            setattr(
                models_mod, cls_name,
                type(cls_name, (), {"__init__": lambda self, *a, **kw: None}),
            )
        _install_stub_module(
            "qdrant_client", AsyncQdrantClient=_StubAsyncQdrantClient, models=models_mod
        )

    try:
        import redis.asyncio  # noqa: F401
    except ImportError:
        class _StubRedis:
            def __init__(self, *a, **kw) -> None:
                pass

            async def get(self, key):
                return None

            async def set(self, key, value, ex=None):
                return True

            async def aclose(self) -> None:
                pass

        asyncio_mod = _install_stub_module(
            "redis.asyncio", Redis=_StubRedis, from_url=lambda *a, **kw: _StubRedis()
        )
        _install_stub_module("redis", asyncio=asyncio_mod)

    try:
        import asyncpg  # noqa: F401
    except ImportError:
        class _StubConnection:
            pass

        class _StubPool:
            pass

        async def _create_pool(*a, **kw):
            return _StubPool()

        _install_stub_module(
            "asyncpg", Pool=_StubPool, Connection=_StubConnection, create_pool=_create_pool
        )

    try:
        import fastembed  # noqa: F401
    except ImportError:
        class _StubTextEmbedding:
            def __init__(self, *a, **kw) -> None:
                pass

        _install_stub_module("fastembed", TextEmbedding=_StubTextEmbedding)


_ensure_heavy_stubs()

import httpx  # noqa: E402

from app import llm  # noqa: E402
from app.agents import orchestrator  # noqa: E402
from app.schemas import ConciergeRequest, StructuredQuery  # noqa: E402

pytestmark = pytest.mark.asyncio


# ── Fake httpx plumbing (no network) ─────────────────────────────────────────
class _FakeStreamResponse:
    def __init__(self, status_code: int, lines: list[str]):
        self.status_code = status_code
        self._lines = lines

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            req = httpx.Request("POST", "https://test.invalid")
            raise httpx.HTTPStatusError(
                f"status {self.status_code}",
                request=req,
                response=httpx.Response(self.status_code, request=req),
            )

    async def aread(self) -> bytes:
        return b""

    async def aiter_lines(self):
        for line in self._lines:
            # A real explicit await point per line: proves callers can act on
            # earlier lines before later ones have arrived (no buffering).
            await asyncio.sleep(0)
            yield line


class _FakeStreamCtx:
    def __init__(self, response: _FakeStreamResponse):
        self._response = response

    async def __aenter__(self) -> _FakeStreamResponse:
        return self._response

    async def __aexit__(self, *exc_info) -> bool:
        return False


class _FakeAsyncClient:
    """Replaces `httpx.AsyncClient` entirely (not just `.send()`).

    `_gemini_stream`/`_anthropic_stream` construct a NEW `httpx.AsyncClient`
    per retry attempt (`async with httpx.AsyncClient(...) as client:`), so the
    factory below hands back the SAME `_FakeAsyncClient` every call — sharing
    one response queue/cursor and one call log across attempts is exactly what
    a retry-then-succeed scenario needs.
    """

    def __init__(self, responses: list[_FakeStreamResponse]):
        self._responses = responses
        self.calls: list[tuple[str, str, dict]] = []

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    def stream(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self._responses[len(self.calls) - 1]
        return _FakeStreamCtx(response)


def _patch_client(monkeypatch: pytest.MonkeyPatch, responses: list[_FakeStreamResponse]) -> _FakeAsyncClient:
    fake_client = _FakeAsyncClient(responses)
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda *a, **kw: fake_client)
    return fake_client


def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry tests must not actually sleep ~0.4-4s per attempt."""
    async def _instant(_attempt: int) -> None:
        return None

    monkeypatch.setattr(llm, "_backoff_sleep", _instant)


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}"


def _gemini_text_frame(text: str, usage: tuple[int, int] | None = None) -> dict:
    frame: dict = {"candidates": [{"content": {"parts": [{"text": text}]}}]}
    if usage is not None:
        frame["usageMetadata"] = {
            "promptTokenCount": usage[0],
            "candidatesTokenCount": usage[1],
        }
    return frame


def _gemini_usage_only_frame(prompt_tokens: int, candidate_tokens: int) -> dict:
    return {
        "usageMetadata": {
            "promptTokenCount": prompt_tokens,
            "candidatesTokenCount": candidate_tokens,
        }
    }


# ── 1) Gemini: usage on the final frame, no usage on earlier frames ──────────
async def test_gemini_stream_final_frame_usage_and_incremental_yield(monkeypatch):
    lines = [
        _sse(_gemini_text_frame("Hel")),
        _sse(_gemini_text_frame("lo")),
        _sse(_gemini_usage_only_frame(prompt_tokens=10, candidate_tokens=2)),
    ]
    _patch_client(monkeypatch, [_FakeStreamResponse(200, lines)])

    stream = llm.stream_text_with_usage("hi", "sys")

    # Pull tokens one at a time; usage must NOT be known before its frame has
    # actually arrived — proves the generator isn't buffering the whole
    # response before yielding.
    tok1 = await stream.__anext__()
    assert tok1 == "Hel"
    assert stream.measured is False

    tok2 = await stream.__anext__()
    assert tok2 == "lo"
    assert stream.measured is False

    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()

    assert stream.measured is True
    assert stream.usage == llm.Usage(input_tokens=10, output_tokens=2)


# ── 2) Gemini: no usageMetadata anywhere -> not measured (proxy fallback) ────
async def test_gemini_stream_without_usage_metadata_is_not_measured(monkeypatch):
    lines = [
        _sse(_gemini_text_frame("no ")),
        _sse(_gemini_text_frame("usage ")),
        _sse(_gemini_text_frame("here")),
    ]
    _patch_client(monkeypatch, [_FakeStreamResponse(200, lines)])

    stream = llm.stream_text_with_usage("hi")
    chunks = [tok async for tok in stream]

    assert chunks == ["no ", "usage ", "here"]
    assert stream.measured is False
    assert stream.usage == llm.Usage()  # still zero — never a false measurement


# ── 3) Gemini: multiple usage frames -> the LAST one wins (not summed) ───────
async def test_gemini_stream_final_frame_is_authoritative_not_cumulative(monkeypatch):
    lines = [
        _sse(_gemini_text_frame("A", usage=(10, 1))),
        _sse(_gemini_text_frame("B", usage=(10, 3))),
        _sse(_gemini_usage_only_frame(prompt_tokens=10, candidate_tokens=6)),
    ]
    _patch_client(monkeypatch, [_FakeStreamResponse(200, lines)])

    stream = llm.stream_text_with_usage("hi")
    chunks = [tok async for tok in stream]

    assert chunks == ["A", "B"]
    assert stream.measured is True
    # If usage were (wrongly) summed across frames this would be 1+3+6=10.
    assert stream.usage == llm.Usage(input_tokens=10, output_tokens=6)


# ── 4) Anthropic: message_start + message_delta usage, text via content_block_delta
async def test_anthropic_stream_usage_from_message_start_and_delta(monkeypatch):
    lines = [
        _sse({"type": "message_start", "message": {"usage": {"input_tokens": 42, "output_tokens": 1}}}),
        _sse({"type": "content_block_start"}),
        _sse({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi "}}),
        _sse({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "there"}}),
        _sse({"type": "content_block_stop"}),
        _sse({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 7}}),
        _sse({"type": "message_stop"}),
    ]
    _patch_client(monkeypatch, [_FakeStreamResponse(200, lines)])
    monkeypatch.setattr(llm.settings, "anthropic_api_key", "test-key")

    stream = llm.stream_text_with_usage("hi", provider="anthropic")
    chunks = [tok async for tok in stream]

    assert chunks == ["Hi ", "there"]
    assert stream.measured is True
    # input_tokens comes from message_start; output_tokens is the message_delta
    # FINAL total (7), not message_start's provisional 1.
    assert stream.usage == llm.Usage(input_tokens=42, output_tokens=7)


# ── 5) Anthropic: retry on 429 then succeed (the asymmetry fixed by WS0-D) ───
async def test_anthropic_stream_retries_on_429_then_succeeds(monkeypatch):
    _no_backoff(monkeypatch)
    monkeypatch.setattr(llm.settings, "anthropic_api_key", "test-key")

    ok_lines = [
        _sse({"type": "message_start", "message": {"usage": {"input_tokens": 5, "output_tokens": 0}}}),
        _sse({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "ok"}}),
        _sse({"type": "message_delta", "delta": {}, "usage": {"output_tokens": 3}}),
    ]
    fake_client = _patch_client(
        monkeypatch,
        [
            _FakeStreamResponse(429, []),  # first attempt: rate-limited
            _FakeStreamResponse(200, ok_lines),  # retry succeeds
        ],
    )

    stream = llm.stream_text_with_usage("hi", provider="anthropic")
    chunks = [tok async for tok in stream]

    assert chunks == ["ok"]
    assert stream.measured is True
    assert stream.usage == llm.Usage(input_tokens=5, output_tokens=3)
    assert len(fake_client.calls) == 2  # proves a retry actually happened


# ── 5b) Gemini retry path still works with the new model/usage_state params ──
async def test_gemini_stream_retries_on_429_then_succeeds(monkeypatch):
    _no_backoff(monkeypatch)
    ok_lines = [_sse(_gemini_text_frame("done", usage=(1, 1)))]
    fake_client = _patch_client(
        monkeypatch, [_FakeStreamResponse(503, []), _FakeStreamResponse(200, ok_lines)]
    )

    stream = llm.stream_text_with_usage("hi")
    chunks = [tok async for tok in stream]

    assert chunks == ["done"]
    assert stream.measured is True
    assert len(fake_client.calls) == 2


# ── 6) model/provider override reaches the actual request ───────────────────
async def test_stream_text_with_usage_model_override_used_in_request(monkeypatch):
    lines = [_sse(_gemini_text_frame("x"))]
    fake_client = _patch_client(monkeypatch, [_FakeStreamResponse(200, lines)])

    stream = llm.stream_text_with_usage("hi", model="gemini-benchmark-special")
    _ = [tok async for tok in stream]

    method, url, kwargs = fake_client.calls[0]
    assert "gemini-benchmark-special:streamGenerateContent" in url


# ── 7) stream_text() (plain-text back-compat surface) still yields correctly ─
async def test_stream_text_backward_compatible_plain_iterator(monkeypatch):
    lines = [_sse(_gemini_text_frame("plain")), _sse(_gemini_text_frame(" text"))]
    _patch_client(monkeypatch, [_FakeStreamResponse(200, lines)])

    out = []
    async for tok in llm.stream_text("hi", "sys"):
        out.append(tok)
    assert out == ["plain", " text"]


# ── Orchestrator wiring ───────────────────────────────────────────────────────
class _FakeUsageStream:
    """Minimal stand-in for `llm.TextStream` used to drive orchestrator.py's
    answer-streaming branch without touching the network or app.llm at all —
    isolates the "wire usage into the trace" behaviour from the frame-parsing
    behaviour already covered by the tests above.
    """

    def __init__(self, tokens: list[str], usage: "llm.Usage", measured: bool):
        self._iter = iter(tokens)
        self.usage = usage
        self.measured = measured

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


async def _fake_parse_intent(query, step=None):
    return StructuredQuery()


async def _fake_retrieve(sq, limit=10):
    return []


def _install_capturing_trace(monkeypatch: pytest.MonkeyPatch) -> list:
    captured: list = []

    class _CapturingTrace(orchestrator.RequestTrace):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            captured.append(self)

    monkeypatch.setattr(orchestrator, "RequestTrace", _CapturingTrace)
    return captured


def _answer_step(trace) -> "orchestrator.AgentStep":
    return next(s for s in trace.steps if s.agent == "answer")


async def test_orchestrator_uses_measured_usage_when_available(monkeypatch):
    monkeypatch.setattr(orchestrator.intent, "parse_intent", _fake_parse_intent)
    monkeypatch.setattr(orchestrator.retrieval, "retrieve", _fake_retrieve)
    captured = _install_capturing_trace(monkeypatch)

    fake_stream = _FakeUsageStream(["Hello", " world"], llm.Usage(input_tokens=50, output_tokens=8), measured=True)
    monkeypatch.setattr(orchestrator.llm, "stream_text_with_usage", lambda *a, **kw: fake_stream)

    req = ConciergeRequest(query="find me a place in lisbon")
    token_events = []
    async for ev in orchestrator.run_concierge(req):
        if ev.get("type") == "token":
            token_events.append(ev["text"])

    assert token_events == ["Hello", " world"]  # streamed incrementally at the SSE layer too

    step = _answer_step(captured[-1])
    assert step.status == "done"
    assert step.input_tokens == 50
    assert step.output_tokens == 8  # exact, from provider usage — NOT chunk_count (2)
    assert step.data == {"usage_source": "measured"}


async def test_orchestrator_falls_back_to_chunk_proxy_when_usage_unmeasured(monkeypatch):
    monkeypatch.setattr(orchestrator.intent, "parse_intent", _fake_parse_intent)
    monkeypatch.setattr(orchestrator.retrieval, "retrieve", _fake_retrieve)
    captured = _install_capturing_trace(monkeypatch)

    fake_stream = _FakeUsageStream(["a", "b", "c", "d"], llm.Usage(), measured=False)
    monkeypatch.setattr(orchestrator.llm, "stream_text_with_usage", lambda *a, **kw: fake_stream)

    req = ConciergeRequest(query="find me a place in lisbon")
    async for _ev in orchestrator.run_concierge(req):
        pass

    step = _answer_step(captured[-1])
    assert step.status == "done"
    # Fallback proxy = number of streamed chunks, never a false zero.
    assert step.output_tokens == 4
    assert step.data == {"usage_source": "estimated"}


# ── retryable status set ────────────────────────────────────────────────────

def test_anthropic_overloaded_529_is_retryable():
    """529 is Anthropic's "Overloaded" — transient, so it must retry.

    It sits outside the contiguous 5xx block most retry lists are written
    against, so it was omitted while 503 (the same class of fault) was covered.
    The Anthropic path therefore failed outright exactly when Anthropic was
    busy, which is when retrying matters most. Gemini never returns 529, so
    nothing in this repo exercised the gap.
    """
    from app import llm

    assert 529 in llm._RETRY_STATUS


def test_retry_set_covers_the_transient_faults_and_not_client_errors():
    from app import llm

    for transient in (429, 500, 502, 503, 504, 529):
        assert transient in llm._RETRY_STATUS, transient
    # Retrying these would just burn the budget on a request that cannot succeed.
    for permanent in (400, 401, 403, 404, 422):
        assert permanent not in llm._RETRY_STATUS, permanent
