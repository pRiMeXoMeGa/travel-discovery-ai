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
from dataclasses import dataclass
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


async def _backoff_sleep(attempt: int) -> None:
    # Exponential backoff with jitter: ~0.4s, 0.8s, 1.6s (+/- jitter).
    delay = min(0.4 * (2 ** attempt), 4.0)
    await asyncio.sleep(delay + random.uniform(0, 0.25))


# ── Public API ────────────────────────────────────────────────────────────────
async def complete_text(prompt: str, system: str | None = None) -> str:
    """Plain text completion (non-streaming)."""
    text, _ = await complete_text_with_usage(prompt, system)
    return text


async def complete_text_with_usage(
    prompt: str, system: str | None = None
) -> tuple[str, Usage]:
    if settings.llm_provider == "anthropic":
        return await _anthropic_complete(prompt, system, response_json=False)
    return await _gemini_complete(prompt, system, response_json=False)


async def complete_json(
    prompt: str, schema: dict, system: str | None = None
) -> dict:
    """Return a structured object. `schema` documents the expected shape and is
    embedded in the prompt as guidance; the provider is asked for JSON mime type.

    On a parse failure we issue ONE repair retry (asking the model to emit valid
    JSON only), then raise LLMError.
    """
    obj, _ = await complete_json_with_usage(prompt, schema, system)
    return obj


async def complete_json_with_usage(
    prompt: str, schema: dict, system: str | None = None
) -> tuple[dict, Usage]:
    schema_hint = json.dumps(schema, indent=2)
    full_prompt = (
        f"{prompt}\n\n"
        "Respond with a SINGLE valid JSON object only — no markdown, code fences, "
        "comments, or surrounding prose. Conform to this schema; use null for "
        "unknown scalar fields and [] for unknown arrays rather than inventing "
        "values:\n"
        f"{schema_hint}"
    )

    if settings.llm_provider == "anthropic":
        raw, usage = await _anthropic_complete(full_prompt, system, response_json=True)
    else:
        raw, usage = await _gemini_complete(full_prompt, system, response_json=True)

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
    if settings.llm_provider == "anthropic":
        raw2, usage2 = await _anthropic_complete(repair_prompt, None, response_json=True)
    else:
        raw2, usage2 = await _gemini_complete(repair_prompt, None, response_json=True)

    parsed = _try_parse_json(raw2)
    if parsed is not None:
        usage.input_tokens += usage2.input_tokens
        usage.output_tokens += usage2.output_tokens
        return parsed, usage

    raise LLMError("complete_json: model did not return parseable JSON after repair")


async def stream_text(prompt: str, system: str | None = None) -> AsyncIterator[str]:
    """Stream a plain-text completion token-by-token (for concierge answers)."""
    if settings.llm_provider == "anthropic":
        async for chunk in _anthropic_stream(prompt, system):
            yield chunk
    else:
        async for chunk in _gemini_stream(prompt, system):
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
    prompt: str, system: str | None, response_json: bool
) -> tuple[str, Usage]:
    if not settings.gemini_api_key:
        raise LLMError("GEMINI_API_KEY is not configured")
    url = f"{_GEMINI_BASE}/{settings.gemini_model}:generateContent"
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
    prompt: str, system: str | None
) -> AsyncIterator[str]:
    if not settings.gemini_api_key:
        raise LLMError("GEMINI_API_KEY is not configured")
    url = f"{_GEMINI_BASE}/{settings.gemini_model}:streamGenerateContent"
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
    prompt: str, system: str | None, response_json: bool
) -> tuple[str, Usage]:
    if not settings.anthropic_api_key:
        raise LLMError("ANTHROPIC_API_KEY is not configured")
    body: dict[str, Any] = {
        "model": settings.anthropic_model,
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


async def _anthropic_stream(prompt: str, system: str | None) -> AsyncIterator[str]:
    if not settings.anthropic_api_key:
        raise LLMError("ANTHROPIC_API_KEY is not configured")
    body: dict[str, Any] = {
        "model": settings.anthropic_model,
        "max_tokens": 1024,
        "stream": True,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        body["system"] = system
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        async with client.stream(
            "POST", _ANTHROPIC_URL, headers=_anthropic_headers(), json=body
        ) as resp:
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
                if frame.get("type") == "content_block_delta":
                    delta = frame.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        yield delta.get("text", "")
