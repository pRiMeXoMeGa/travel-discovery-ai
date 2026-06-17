"""LLM provider abstraction with streaming + structured output.

Default provider is Gemini Flash (free tier); Anthropic Claude Haiku is the
paid fallback. Keep all model access behind this module so agents are
provider-agnostic.
"""
from typing import AsyncIterator

from .config import settings


async def stream_text(prompt: str, system: str | None = None) -> AsyncIterator[str]:
    """Stream a plain-text completion token-by-token (for concierge answers)."""
    if settings.llm_provider == "anthropic":
        async for chunk in _stream_anthropic(prompt, system):
            yield chunk
    else:
        async for chunk in _stream_gemini(prompt, system):
            yield chunk


async def complete_json(prompt: str, schema: dict, system: str | None = None) -> dict:
    """Return a structured object constrained to `schema`.

    Used by the Intent agent (NL -> structured query) and anywhere we need
    machine-readable output. TODO: wire provider-native structured output
    (Gemini response_schema / Anthropic tool-use) and validate against `schema`.
    """
    raise NotImplementedError("TODO: structured JSON completion")


# ── Gemini ───────────────────────────────────────────────────────────────────
async def _stream_gemini(prompt: str, system: str | None) -> AsyncIterator[str]:
    # TODO: implement with google.generativeai async streaming.
    # import google.generativeai as genai
    # genai.configure(api_key=settings.gemini_api_key)
    # model = genai.GenerativeModel(settings.gemini_model, system_instruction=system)
    # async for chunk in await model.generate_content_async(prompt, stream=True):
    #     yield chunk.text
    raise NotImplementedError("TODO: Gemini streaming")
    yield ""  # pragma: no cover  (keeps this an async generator)


# ── Anthropic (fallback) ──────────────────────────────────────────────────────
async def _stream_anthropic(prompt: str, system: str | None) -> AsyncIterator[str]:
    # TODO: implement with anthropic.AsyncAnthropic().messages.stream(...)
    raise NotImplementedError("TODO: Anthropic streaming")
    yield ""  # pragma: no cover
