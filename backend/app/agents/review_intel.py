"""Review intelligence agent: synthesize review insights with citations.

For a property (or small candidate set) summarize what guests consistently
praise / occasionally complain about, with EVERY claim traceable to an actual
review row (brief §2.3, grounding/§3).

Grounding contract:
  * The model is given ONLY the retrieved review snippets (each tagged with its
    review id) and the precomputed aspect averages. It is instructed to cite
    review ids and to abstain when evidence is insufficient — no fabrication.
  * Returned Citation objects point at real `reviews.id` rows; the orchestrator
    can render id + snippet.
  * Non-English reviews have null aspects (English-heavy enrichment) — handled
    gracefully: aspect stats simply omit nulls; snippets still surface.

Caching: syntheses are cached in Redis keyed by the listing set + focus.
"""
import hashlib
import json
import logging
from typing import Any

from .. import llm
from ..cache import cache_get, cache_set
from ..db import get_pool
from ..embeddings import embed_query
from ..observability import AgentStep
from ..schemas import Citation
from ..vectorstore import get_qdrant
from ..config import settings

logger = logging.getLogger(__name__)

_MAX_SNIPPETS = 8        # bounded evidence set per synthesis (lean LLM input)
_SNIPPET_CHARS = 280

_SYSTEM = (
    "You are a review-intelligence analyst. From the provided guest reviews, write "
    "a short, balanced summary of what guests CONSISTENTLY praise and what they "
    "OCCASIONALLY complain about. Absolute rules:\n"
    "1. Use ONLY the provided reviews as evidence — never invent details, names, "
    "or numbers.\n"
    "2. Cite the review id (e.g. [r3]) immediately after each claim it supports.\n"
    "3. Reviews may be in different languages; read them all and write the summary "
    "in English (you may briefly quote a short translated phrase).\n"
    "4. Weight by frequency: say 'consistently' only for themes spanning several "
    "reviews, 'a few guests' for one-offs. If evidence is thin or contradictory, "
    "say so honestly rather than overstating.\n"
    "5. If there are no reviews, state that no review evidence is available and "
    "make no claims.\n"
    "Keep it to 3-5 sentences."
)


def _cache_key(listing_ids: list[str], focus: str | None) -> str:
    raw = json.dumps({"ids": sorted(listing_ids), "focus": focus or ""}, sort_keys=True)
    return "review_intel:" + hashlib.sha256(raw.encode()).hexdigest()


async def _fetch_aspect_summary(listing_ids: list[str]) -> dict[str, Any]:
    """Pull precomputed per-property summary + aspect_avg from Postgres."""
    if not listing_ids:
        return {}
    placeholders = ", ".join(f"${i + 1}" for i in range(len(listing_ids)))
    sql = (
        "SELECT listing_id, summary, aspect_avg FROM listing_summaries "
        f"WHERE listing_id IN ({placeholders})"
    )
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *listing_ids)
    return {r["listing_id"]: {"summary": r["summary"], "aspect_avg": r["aspect_avg"]} for r in rows}


async def _retrieve_review_snippets(
    listing_ids: list[str], focus: str | None
) -> list[dict]:
    """Return up to _MAX_SNIPPETS relevant reviews with text, grounded in real rows.

    Option A: reviews are NOT vector-embedded — they live in Postgres with a
    full-text index (`idx_reviews_fts`, 'simple' config). So when a `focus` is
    given we rank this property's reviews by Postgres full-text relevance; without
    a focus (or no FTS match) we fall back to the highest- AND lowest-rated reviews
    so praise and complaints are both represented. All grounded in real rows.
    """
    n = len(listing_ids)
    id_ph = ", ".join(f"${i + 1}" for i in range(n))
    pool = await get_pool()
    rows = []

    if focus and focus.strip():
        # Full-text rank within these listings' reviews. 'simple' config matches
        # the GIN index and is language-agnostic (reviews are multilingual).
        focus_ph = f"${n + 1}"
        sql = (
            "SELECT id, listing_id, rating, text, language, sentiment "
            f"FROM reviews WHERE listing_id IN ({id_ph}) "
            f"AND to_tsvector('simple', text) @@ plainto_tsquery('simple', {focus_ph}) "
            f"ORDER BY ts_rank(to_tsvector('simple', text), plainto_tsquery('simple', {focus_ph})) DESC "
            f"LIMIT {_MAX_SNIPPETS}"
        )
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *listing_ids, focus)

    if not rows:
        # Balanced sample: top + bottom rated for these listings.
        half = max(1, _MAX_SNIPPETS // 2)
        sql = (
            "(SELECT id, listing_id, rating, text, language, sentiment FROM reviews "
            f" WHERE listing_id IN ({id_ph}) ORDER BY rating DESC NULLS LAST "
            f" LIMIT {half}) UNION ALL "
            "(SELECT id, listing_id, rating, text, language, sentiment FROM reviews "
            f" WHERE listing_id IN ({id_ph}) ORDER BY rating ASC NULLS LAST "
            f" LIMIT {half})"
        )
        # Both UNION halves reference the same $1..$n placeholders, so the
        # listing ids are passed ONCE (not twice).
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *listing_ids)

    seen: set[str] = set()
    snippets: list[dict] = []
    for r in rows:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        snippets.append(
            {
                "id": r["id"],
                "rating": float(r["rating"]) if r["rating"] is not None else None,
                "language": r["language"],
                "text": (r["text"] or "")[:_SNIPPET_CHARS],
            }
        )
    return snippets[:_MAX_SNIPPETS]


def _format_aspects(aspect_summary: dict[str, Any]) -> str:
    lines: list[str] = []
    for lid, data in aspect_summary.items():
        avg = data.get("aspect_avg")
        if isinstance(avg, dict) and avg:
            # drop nulls (non-English reviews lack aspect scores)
            clean = {k: round(float(v), 2) for k, v in avg.items() if v is not None}
            if clean:
                lines.append(f"  aspect averages: {clean}")
    return "\n".join(lines)


async def synthesize(
    listing_ids: list[str], focus: str | None = None, step: AgentStep | None = None
) -> tuple[str, list[Citation]]:
    """Return (synthesis_text, citations).

    Always grounded: the LLM only sees the retrieved snippets. If no reviews
    exist, returns an honest no-evidence message with no citations (no LLM call).
    """
    if not listing_ids:
        return "No property was specified, so there is no review evidence to summarize.", []

    key = _cache_key(listing_ids, focus)
    try:
        cached = await cache_get(key)
        if cached:
            return cached["text"], [Citation(**c) for c in cached["citations"]]
    except Exception as exc:  # noqa: BLE001
        logger.warning("review_intel cache_get failed: %s", exc)

    snippets = await _retrieve_review_snippets(listing_ids, focus)

    if not snippets:
        text = (
            "No guest reviews are available for this property yet, so I can't make "
            "any claims about what guests praise or complain about."
        )
        return text, []

    aspect_summary = await _fetch_aspect_summary(listing_ids)
    aspect_block = _format_aspects(aspect_summary)

    # Build the grounded evidence block with stable [r#] labels mapped to ids.
    label_to_id: dict[str, str] = {}
    evidence_lines: list[str] = []
    for i, s in enumerate(snippets, start=1):
        label = f"r{i}"
        label_to_id[label] = s["id"]
        rating = f"{s['rating']:.1f}" if s["rating"] is not None else "n/a"
        evidence_lines.append(
            f"[{label}] (rating {rating}, lang {s['language']}): {s['text']}"
        )
    evidence = "\n".join(evidence_lines)

    focus_line = f"Focus the summary on: {focus}\n" if focus else ""
    prompt = (
        f"{focus_line}"
        "Synthesize these guest reviews. Cite the [r#] label after each claim.\n\n"
        f"REVIEWS:\n{evidence}\n"
        + (f"\nAGGREGATE ASPECT SCORES (0-1, higher=better):\n{aspect_block}\n" if aspect_block else "")
    )

    try:
        text, usage = await llm.complete_text_with_usage(prompt, _SYSTEM)
        if step is not None:
            step.input_tokens += usage.input_tokens
            step.output_tokens += usage.output_tokens
    except llm.LLMError as exc:
        logger.warning("review_intel: LLM failed (%s) — falling back to aspect stats", exc)
        # Graceful degradation: deterministic, still grounded.
        text = _fallback_summary(snippets, aspect_summary)
        if step is not None:
            step.detail = f"llm_error: {exc}"

    # Citations: only the snippets actually referenced by a [r#] label, else all.
    cited_labels = [lab for lab in label_to_id if f"[{lab}]" in text or f"{lab}]" in text]
    used = cited_labels or list(label_to_id.keys())
    snippet_by_id = {s["id"]: s for s in snippets}
    citations = [
        Citation(kind="review", id=label_to_id[lab], snippet=snippet_by_id[label_to_id[lab]]["text"])
        for lab in used
    ]

    try:
        await cache_set(
            key,
            {"text": text, "citations": [c.model_dump() for c in citations]},
            ttl=settings.cache_ttl_seconds,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("review_intel cache_set failed: %s", exc)

    return text, citations


def _fallback_summary(snippets: list[dict], aspect_summary: dict) -> str:
    """Deterministic grounded summary used when the LLM is unavailable."""
    rated = [s for s in snippets if s["rating"] is not None]
    pos = [s for s in rated if s["rating"] >= 4.0]
    neg = [s for s in rated if s["rating"] <= 3.0]
    parts = [f"Based on {len(snippets)} retrieved reviews:"]
    if pos:
        parts.append(f"{len(pos)} are positive (e.g. [{pos[0]['id'][:8]}…]).")
    if neg:
        parts.append(f"{len(neg)} raise concerns (e.g. [{neg[0]['id'][:8]}…]).")
    if not pos and not neg:
        parts.append("ratings are mixed or unavailable; see cited snippets.")
    return " ".join(parts)
