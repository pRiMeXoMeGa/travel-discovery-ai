"""LLM provider abstraction with streaming + structured output.

Default provider is Gemini Flash (free tier); Anthropic Claude Haiku is the
paid fallback. Keep all model access behind this module so agents are
provider-agnostic.

IMPORTANT: we deliberately do NOT use the deprecated `google.generativeai`
SDK. All Gemini access goes through the REST API over httpx (already a
dependency), using the v1beta `generateContent` / `streamGenerateContent`
endpoints. Structured output is requested via
`generationConfig.responseMimeType = "application/json"`.

Resilience contract (every call):
  * bounded timeouts (httpx.Timeout)
  * retry-on-429 / 5xx with short exponential backoff + jitter
  * one repair retry for malformed structured JSON, then raise
  * usage metadata (prompt/candidate token counts) surfaced where available
"""
import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from .config import settings

logger = logging.getLogger(__name__)

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

# Bounded timeouts: connect fast, allow generous read for generation.
_TIMEOUT = httpx.Timeout(connect=5.0, read=45.0, write=10.0, pool=5.0)
_MAX_RETRIES = 3
_RETRY_STATUS = {429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    """Raised when an LLM call exhausts retries or returns unusable output."""


@dataclass
class Usage:
    """Token usage for a single LLM call (best-effort; provider-dependent)."""
    input_tokens: int = 0
    output_tokens: int = 0


# Last-usage is exposed via the helpers' return tuples; agents that want token
# accounting call the *_with_usage variants. The plain helpers stay ergonomic.


@dataclass
class _StreamUsageState:
    """Mutable box a provider stream fills in-place as usage frames arrive.

    Shared by reference between the provider generator and the `TextStream`
    wrapper so usage is visible to the caller the moment the generator updates
    it — no buffering of the response is required to "read usage at the end".
    `measured` flips true only once real provider-reported usage has been
    seen, so callers can tell "no usage on this provider/frame shape" (proxy
    fallback) apart from "usage is simply still zero".
    """
    usage: Usage = field(default_factory=Usage)
    measured: bool = False


class TextStream:
    """Async iterator of text chunks that exposes `.usage` / `.measured`.

    Wraps a provider's async generator so `async for tok in stream` still
    yields incrementally (tokens are never buffered), while `stream.usage`
    reflects the latest usage the provider has reported once the loop
    finishes. Existing callers that only want plain text keep using
    `stream_text()`, which iterates one of these internally and discards the
    usage — so nothing about the plain-text call sites needs to change.
    """

    def __init__(self, agen: AsyncIterator[str], state: _StreamUsageState):
        self._agen = agen
        self._state = state

    def __aiter__(self) -> "TextStream":
        return self

    async def __anext__(self) -> str:
        return await self._agen.__anext__()

    @property
    def usage(self) -> Usage:
        return self._state.usage

    @property
    def measured(self) -> bool:
        return self._state.measured


def _resolve_provider(provider: str | None) -> str:
    return provider or settings.llm_provider


def _resolve_model(resolved_provider: str, model: str | None) -> str:
    if model:
        return model
    return settings.anthropic_model if resolved_provider == "anthropic" else settings.gemini_model


async def _backoff_sleep(attempt: int) -> None:
    # Exponential backoff with jitter: ~0.4s, 0.8s, 1.6s (+/- jitter).
    delay = min(0.4 * (2 ** attempt), 4.0)
    await asyncio.sleep(delay + random.uniform(0, 0.25))


# ── Public API ────────────────────────────────────────────────────────────────
# Every public function below accepts optional keyword-only `model` / `provider`
# overrides, defaulting to `settings.llm_provider` / the provider's configured
# model — existing call sites that pass neither are unaffected. This lets a
# benchmark harness (WS5) compare providers/models within one process instead
# of restarting between each.
async def complete_text(
    prompt: str,
    system: str | None = None,
    *,
    model: str | None = None,
    provider: str | None = None,
) -> str:
    """Plain text completion (non-streaming)."""
    text, _ = await complete_text_with_usage(prompt, system, model=model, provider=provider)
    return text


async def complete_text_with_usage(
    prompt: str,
    system: str | None = None,
    *,
    model: str | None = None,
    provider: str | None = None,
) -> tuple[str, Usage]:
    p = _resolve_provider(provider)
    m = _resolve_model(p, model)
    if p == "anthropic":
        return await _anthropic_complete(prompt, system, response_json=False, model=m)
    return await _gemini_complete(prompt, system, response_json=False, model=m)


async def complete_json(
    prompt: str,
    schema: dict,
    system: str | None = None,
    *,
    model: str | None = None,
    provider: str | None = None,
) -> dict:
    """Return a structured object. `schema` documents the expected shape and is
    embedded in the prompt as guidance; the provider is asked for JSON mime type.

    On a parse failure we issue ONE repair retry (asking the model to emit valid
    JSON only), then raise LLMError.
    """
    obj, _ = await complete_json_with_usage(prompt, schema, system, model=model, provider=provider)
    return obj


async def complete_json_with_usage(
    prompt: str,
    schema: dict,
    system: str | None = None,
    *,
    model: str | None = None,
    provider: str | None = None,
) -> tuple[dict, Usage]:
    p = _resolve_provider(provider)
    m = _resolve_model(p, model)
    schema_hint = json.dumps(schema, indent=2)
    full_prompt = (
        f"{prompt}\n\n"
        "Respond with a SINGLE valid JSON object only — no markdown, code fences, "
        "comments, or surrounding prose. Conform to this schema; use null for "
        "unknown scalar fields and [] for unknown arrays rather than inventing "
        "values:\n"
        f"{schema_hint}"
    )

    if p == "anthropic":
        raw, usage = await _anthropic_complete(full_prompt, system, response_json=True, model=m)
    else:
        raw, usage = await _gemini_complete(full_prompt, system, response_json=True, model=m)

    parsed = _try_parse_json(raw)
    if parsed is not None:
        return parsed, usage

    # ── one repair pass ──
    logger.warning("complete_json: first parse failed, attempting repair")
    repair_prompt = (
        "The following text was supposed to be a single valid JSON object but "
        "could not be parsed. Return ONLY the corrected JSON object, nothing "
        f"else:\n\n{raw}"
    )
    if p == "anthropic":
        raw2, usage2 = await _anthropic_complete(repair_prompt, None, response_json=True, model=m)
    else:
        raw2, usage2 = await _gemini_complete(repair_prompt, None, response_json=True, model=m)

    parsed = _try_parse_json(raw2)
    if parsed is not None:
        usage.input_tokens += usage2.input_tokens
        usage.output_tokens += usage2.output_tokens
        return parsed, usage

    raise LLMError("complete_json: model did not return parseable JSON after repair")


def stream_text_with_usage(
    prompt: str,
    system: str | None = None,
    *,
    model: str | None = None,
    provider: str | None = None,
) -> TextStream:
    """Stream text plus the real token usage, once available.

    Returns a `TextStream` immediately (no `await` — same call shape as calling
    an async-generator function): `async for tok in stream: ...` yields text
    incrementally exactly like `stream_text`, and `stream.usage` /
    `stream.measured` reflect the provider's latest usage frame once the loop
    finishes. `measured` is False when the provider/frame shape never reported
    usage, so callers can fall back to a coarse proxy instead of reporting a
    false zero.
    """
    p = _resolve_provider(provider)
    m = _resolve_model(p, model)
    state = _StreamUsageState()
    if p == "anthropic":
        agen = _anthropic_stream(prompt, system, model=m, usage_state=state)
    else:
        agen = _gemini_stream(prompt, system, model=m, usage_state=state)
    return TextStream(agen, state)


async def stream_text(
    prompt: str,
    system: str | None = None,
    *,
    model: str | None = None,
    provider: str | None = None,
) -> AsyncIterator[str]:
    """Stream a plain-text completion token-by-token (for concierge answers).

    Thin wrapper over `stream_text_with_usage` that discards usage — kept so
    existing `async for tok in llm.stream_text(...)` call sites are untouched.
    """
    stream = stream_text_with_usage(prompt, system, model=model, provider=provider)
    async for chunk in stream:
        yield chunk


# ── JSON parsing helper ───────────────────────────────────────────────────────
def _try_parse_json(raw: str) -> dict | None:
    if not raw:
        return None
    s = raw.strip()
    # Strip accidental markdown fences.
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, ValueError):
        # Last resort: grab the outermost {...}.
        start, end = s.find("{"), s.rfind("}")
        if 0 <= start < end:
            try:
                obj = json.loads(s[start : end + 1])
                return obj if isinstance(obj, dict) else None
            except (json.JSONDecodeError, ValueError):
                return None
        return None


# ── Gemini (REST over httpx) ──────────────────────────────────────────────────
def _gemini_body(prompt: str, system: str | None, response_json: bool) -> dict:
    body: dict[str, Any] = {"contents": [{"parts": [{"text": prompt}]}]}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    gen_cfg: dict[str, Any] = {}
    if response_json:
        gen_cfg["responseMimeType"] = "application/json"
    # Keep outputs lean to conserve free-tier quota.
    gen_cfg["temperature"] = 0.2
    body["generationConfig"] = gen_cfg
    return body


def _gemini_usage(payload: dict) -> Usage:
    meta = payload.get("usageMetadata") or {}
    return Usage(
        input_tokens=int(meta.get("promptTokenCount", 0) or 0),
        output_tokens=int(meta.get("candidatesTokenCount", 0) or 0),
    )


def _gemini_extract_text(payload: dict) -> str:
    candidates = payload.get("candidates") or []
    if not candidates:
        return ""
    parts = (candidates[0].get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts)


async def _gemini_complete(
    prompt: str, system: str | None, response_json: bool, model: str | None = None
) -> tuple[str, Usage]:
    if not settings.gemini_api_key:
        raise LLMError("GEMINI_API_KEY is not configured")
    url = f"{_GEMINI_BASE}/{model or settings.gemini_model}:generateContent"
    params = {"key": settings.gemini_api_key}
    body = _gemini_body(prompt, system, response_json)

    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for attempt in range(_MAX_RETRIES):
            try:
                resp = await client.post(url, params=params, json=body)
                if resp.status_code in _RETRY_STATUS:
                    logger.warning(
                        "Gemini %s (attempt %d/%d)",
                        resp.status_code, attempt + 1, _MAX_RETRIES,
                    )
                    last_exc = LLMError(f"Gemini HTTP {resp.status_code}")
                    await _backoff_sleep(attempt)
                    continue
                resp.raise_for_status()
                payload = resp.json()
                return _gemini_extract_text(payload), _gemini_usage(payload)
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                logger.warning("Gemini request error (attempt %d): %s", attempt + 1, exc)
                await _backoff_sleep(attempt)
    raise LLMError(f"Gemini completion failed after {_MAX_RETRIES} attempts: {last_exc}")


async def _gemini_stream(
    prompt: str,
    system: str | None,
    model: str | None = None,
    usage_state: "_StreamUsageState | None" = None,
) -> AsyncIterator[str]:
    if not settings.gemini_api_key:
        raise LLMError("GEMINI_API_KEY is not configured")
    url = f"{_GEMINI_BASE}/{model or settings.gemini_model}:streamGenerateContent"
    params = {"alt": "sse", "key": settings.gemini_api_key}
    body = _gemini_body(prompt, system, response_json=False)

    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                async with client.stream("POST", url, params=params, json=body) as resp:
                    if resp.status_code in _RETRY_STATUS:
                        await resp.aread()
                        last_exc = LLMError(f"Gemini stream HTTP {resp.status_code}")
                        await _backoff_sleep(attempt)
                        continue
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[len("data:") :].strip()
                        if not data or data == "[DONE]":
                            continue
                        try:
                            frame = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        # usageMetadata is cumulative across frames; the last
                        # frame that carries it is authoritative, so we simply
                        # overwrite on every frame that has one — no buffering
                        # of the response is needed to capture it.
                        if usage_state is not None and frame.get("usageMetadata"):
                            frame_usage = _gemini_usage(frame)
                            usage_state.usage.input_tokens = frame_usage.input_tokens
                            usage_state.usage.output_tokens = frame_usage.output_tokens
                            usage_state.measured = True
                        text = _gemini_extract_text(frame)
                        if text:
                            yield text
            return  # stream completed
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            logger.warning("Gemini stream error (attempt %d): %s", attempt + 1, exc)
            await _backoff_sleep(attempt)
    raise LLMError(f"Gemini streaming failed after {_MAX_RETRIES} attempts: {last_exc}")


# ── Anthropic (fallback; no key set in dev) ──────────────────────────────────
def _anthropic_headers() -> dict:
    return {
        "x-api-key": settings.anthropic_api_key or "",
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }


async def _anthropic_complete(
    prompt: str, system: str | None, response_json: bool, model: str | None = None
) -> tuple[str, Usage]:
    if not settings.anthropic_api_key:
        raise LLMError("ANTHROPIC_API_KEY is not configured")
    body: dict[str, Any] = {
        "model": model or settings.anthropic_model,
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        body["system"] = system

    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for attempt in range(_MAX_RETRIES):
            try:
                resp = await client.post(_ANTHROPIC_URL, headers=_anthropic_headers(), json=body)
                if resp.status_code in _RETRY_STATUS:
                    last_exc = LLMError(f"Anthropic HTTP {resp.status_code}")
                    await _backoff_sleep(attempt)
                    continue
                resp.raise_for_status()
                payload = resp.json()
                text = "".join(
                    b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text"
                )
                u = payload.get("usage") or {}
                usage = Usage(
                    input_tokens=int(u.get("input_tokens", 0) or 0),
                    output_tokens=int(u.get("output_tokens", 0) or 0),
                )
                return text, usage
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                await _backoff_sleep(attempt)
    raise LLMError(f"Anthropic completion failed after {_MAX_RETRIES} attempts: {last_exc}")


def _anthropic_stream_usage(frame: dict, state: "_StreamUsageState") -> None:
    """Apply usage from one Anthropic SSE frame to `state`, in place.

    `message_start` carries the authoritative `input_tokens` (and an initial,
    not-yet-final `output_tokens`); `message_delta` carries the running/final
    `output_tokens` as generation proceeds — the last one seen before
    `message_stop` is the true total, so we simply overwrite each time.
    """
    ftype = frame.get("type")
    if ftype == "message_start":
        usage = (frame.get("message") or {}).get("usage") or {}
        if "input_tokens" in usage:
            state.usage.input_tokens = int(usage.get("input_tokens", 0) or 0)
            state.measured = True
        if "output_tokens" in usage:
            state.usage.output_tokens = int(usage.get("output_tokens", 0) or 0)
            state.measured = True
    elif ftype == "message_delta":
        usage = frame.get("usage") or {}
        if "output_tokens" in usage:
            state.usage.output_tokens = int(usage.get("output_tokens", 0) or 0)
            state.measured = True


async def _anthropic_stream(
    prompt: str,
    system: str | None,
    model: str | None = None,
    usage_state: "_StreamUsageState | None" = None,
) -> AsyncIterator[str]:
    if not settings.anthropic_api_key:
        raise LLMError("ANTHROPIC_API_KEY is not configured")
    body: dict[str, Any] = {
        "model": model or settings.anthropic_model,
        "max_tokens": 1024,
        "stream": True,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        body["system"] = system

    # Retry-on-429/5xx, mirroring `_gemini_stream` — previously this stream had
    # no retry loop at all, so a transient 429/5xx crashed the answer instead
    # of degrading gracefully like every other provider path.
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                async with client.stream(
                    "POST", _ANTHROPIC_URL, headers=_anthropic_headers(), json=body
                ) as resp:
                    if resp.status_code in _RETRY_STATUS:
                        await resp.aread()
                        last_exc = LLMError(f"Anthropic stream HTTP {resp.status_code}")
                        await _backoff_sleep(attempt)
                        continue
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[len("data:") :].strip()
                        if not data:
                            continue
                        try:
                            frame = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if usage_state is not None:
                            _anthropic_stream_usage(frame, usage_state)
                        if frame.get("type") == "content_block_delta":
                            delta = frame.get("delta") or {}
                            if delta.get("type") == "text_delta":
                                yield delta.get("text", "")
            return  # stream completed
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            logger.warning("Anthropic stream error (attempt %d): %s", attempt + 1, exc)
            await _backoff_sleep(attempt)
    raise LLMError(f"Anthropic streaming failed after {_MAX_RETRIES} attempts: {last_exc}")
