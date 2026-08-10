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
    gracefully: aspect stats simply omit nulls; snippets still surface, and the
    sampler never orders/filters on a nullable-for-non-English column.

Evidence selection (WS0-B): with a `focus` we rank by Postgres FTS; without one
we take a balanced praise/complaint sample driven by *aspect polarity presence*
(see `build_polarity_sample_query`). Both query builders are pure functions
returning `(sql, params)` so the sampling contract is unit-testable without a
database — see `backend/tests/test_review_intel.py`.

Caching: syntheses are cached in Redis keyed by the listing set + focus.
"""
import hashlib
import json
import logging
from typing import Any

from .. import llm
from ..cache import cache_get, cache_set
from ..config import settings
from ..db import get_pool
from ..observability import AgentStep
from ..schemas import Citation

logger = logging.getLogger(__name__)

_MAX_SNIPPETS = 8        # bounded evidence set per synthesis (lean LLM input)
_SNIPPET_CHARS = 280

# `reviews.rating` is ALWAYS NULL in this corpus — Inside Airbnb's reviews.csv
# carries no per-review stars (ingestion/ingest.py:805). The only per-review
# signal we have is the heuristic enrichment: `aspects` (JSONB, per-aspect score
# in [-1, 1], key absent/null when the aspect is not mentioned) and `sentiment`
# (the mean of the non-null aspect scores, so also in [-1, 1], and NULL for
# non-English reviews because the keyword dictionaries are English).
# Therefore: sample on aspect polarity presence, never on rating, and never with
# `sentiment ... NULLS LAST` (that would silently make the evidence set
# English-only, contradicting _SYSTEM rule 3).
_SENTIMENT_POS = 0.2     # >= this => "leans positive"
_SENTIMENT_NEG = -0.2    # <= this => "leans negative"

# Column list shared by both retrieval paths so the snippet dict is uniform.
_SNIPPET_COLS = "id, listing_id, text, language, sentiment, aspects"
_HALF = max(1, _MAX_SNIPPETS // 2)   # per-polarity quota in the balanced sample

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
    "6. Each review is tagged with a heuristic sentiment score in [-1, 1] and the "
    "aspects it praises/criticises. These are keyword-derived signals, NOT guest "
    "ratings: use them to weigh emphasis, never quote them as scores or stars, and "
    "never call a review 'unrated'. 'sentiment n/a' means the review is not in "
    "English, not that the guest was neutral.\n"
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


def _id_placeholders(listing_ids: list[str]) -> str:
    """`$1, $2, ...` for an IN () list. `NULL` (matches nothing) when empty."""
    if not listing_ids:
        return "NULL"
    return ", ".join(f"${i + 1}" for i in range(len(listing_ids)))


def build_focus_query(
    listing_ids: list[str], focus: str, limit: int = _MAX_SNIPPETS
) -> tuple[str, list]:
    """Build the (sql, params) that ranks these listings' reviews by FTS relevance.

    Pure — no DB access, so it is unit-testable. 'simple' config matches the GIN
    index (`idx_reviews_fts`) and is language-agnostic (the corpus is
    multilingual). `, id` is appended to the ORDER BY so equal ranks break
    deterministically across runs.
    """
    params: list = list(listing_ids)
    id_ph = _id_placeholders(listing_ids)

    def p(value: object) -> str:
        params.append(value)
        return f"${len(params)}"

    focus_ph = p(focus)
    limit_ph = p(limit)
    tsv = "to_tsvector('simple', text)"
    tsq = f"plainto_tsquery('simple', {focus_ph})"
    sql = (
        f"SELECT {_SNIPPET_COLS} FROM reviews "
        f"WHERE listing_id IN ({id_ph}) AND {tsv} @@ {tsq} "
        f"ORDER BY ts_rank({tsv}, {tsq}) DESC, id "
        f"LIMIT {limit_ph}"
    )
    return sql, params


def build_polarity_sample_query(
    listing_ids: list[str],
    half: int = _HALF,
    limit: int = _MAX_SNIPPETS,
) -> tuple[str, list]:
    """Build the (sql, params) for the no-focus balanced sample.

    Pure — no DB access, so it is unit-testable without Postgres.

    Sampling semantics (replaces the dead `ORDER BY rating` sample, since
    `reviews.rating` is always NULL here):

      bucket 0  up to `half` reviews whose `aspects` JSONB contains ANY score > 0
                (i.e. the guest praised something), strongest praise first;
      bucket 1  up to `half` reviews with ANY score < 0 (a complaint), harshest
                first;
      bucket 2  up to `limit` remaining reviews regardless of polarity, so a
                property whose reviews are all non-English (null aspects) still
                yields evidence instead of an empty set.

    Within every bucket, rows are first ranked *per language* and then ordered by
    that rank, so the first pick from each language comes before the second pick
    from any language — language diversity as a tiebreak, keeping the evidence
    set multilingual (_SYSTEM rule 3). Every ORDER BY ends in `, id`, and the
    outer ORDER BY is `bucket, ord, id`, so the row order is fully deterministic
    (UNION ALL branch order is otherwise not guaranteed).

    Buckets overlap by design (a mixed review is both praise and complaint); the
    caller de-duplicates by id, keeping the first (highest-priority) occurrence.
    """
    params: list = list(listing_ids)
    id_ph = _id_placeholders(listing_ids)

    def p(value: object) -> str:
        params.append(value)
        return f"${len(params)}"

    half_ph = p(half)
    limit_ph = p(limit)

    # Peak aspect score for a row. Guarded by jsonb_typeof so NULL/scalar
    # `aspects` yields NULL (never an error), and non-numeric members are
    # skipped — the enrichment writes JSON null for unmentioned aspects.
    def peak(agg: str) -> str:
        return (
            "CASE WHEN jsonb_typeof(r.aspects) = 'object' THEN "
            f"(SELECT {agg}((e.value)::numeric) FROM jsonb_each(r.aspects) e "
            "WHERE jsonb_typeof(e.value) = 'number') END"
        )

    cols = _SNIPPET_COLS
    sql = f"""
WITH base AS (
    SELECT r.id, r.listing_id, r.text, r.language, r.sentiment, r.aspects,
           {peak('max')} AS pos_peak,
           {peak('min')} AS neg_peak
      FROM reviews r
     WHERE r.listing_id IN ({id_ph})
),
pos_ranked AS (
    SELECT *, row_number() OVER (
               PARTITION BY language ORDER BY pos_peak DESC, id
           ) AS lang_rank
      FROM base WHERE pos_peak > 0
),
pos AS (
    SELECT {cols}, 0 AS bucket,
           row_number() OVER (ORDER BY lang_rank, pos_peak DESC, id) AS ord
      FROM pos_ranked
),
neg_ranked AS (
    SELECT *, row_number() OVER (
               PARTITION BY language ORDER BY neg_peak ASC, id
           ) AS lang_rank
      FROM base WHERE neg_peak < 0
),
neg AS (
    SELECT {cols}, 1 AS bucket,
           row_number() OVER (ORDER BY lang_rank, neg_peak ASC, id) AS ord
      FROM neg_ranked
),
rest_ranked AS (
    SELECT *, row_number() OVER (PARTITION BY language ORDER BY id) AS lang_rank
      FROM base
),
rest AS (
    SELECT {cols}, 2 AS bucket,
           row_number() OVER (ORDER BY lang_rank, id) AS ord
      FROM rest_ranked
)
SELECT {cols}
  FROM (
        SELECT * FROM pos  WHERE ord <= {half_ph}
        UNION ALL
        SELECT * FROM neg  WHERE ord <= {half_ph}
        UNION ALL
        SELECT * FROM rest WHERE ord <= {limit_ph}
       ) u
 ORDER BY bucket, ord, id
""".strip()
    return sql, params


def _split_aspects(aspects: Any) -> tuple[list[str], list[str]]:
    """Split an `aspects` JSONB payload into (praised, criticised) aspect names.

    Tolerant by design: the column is NULL for non-English reviews and individual
    aspects are JSON null when not mentioned. Never raises.
    """
    if not isinstance(aspects, dict):
        return [], []
    praised: list[str] = []
    criticised: list[str] = []
    for name, score in sorted(aspects.items()):
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            continue
        if score > 0:
            praised.append(str(name))
        elif score < 0:
            criticised.append(str(name))
    return praised, criticised


def _row_to_snippet(row: Any) -> dict:
    """Map a review row onto the snippet contract (id/language/text + signal)."""
    praised, criticised = _split_aspects(row["aspects"])
    sentiment = row["sentiment"]
    return {
        "id": row["id"],
        "language": row["language"],
        "text": (row["text"] or "")[:_SNIPPET_CHARS],
        "sentiment": float(sentiment) if sentiment is not None else None,
        "praised": praised,
        "criticised": criticised,
    }


async def _retrieve_review_snippets(
    listing_ids: list[str], focus: str | None
) -> list[dict]:
    """Return up to _MAX_SNIPPETS relevant reviews with text, grounded in real rows.

    Option A: reviews are NOT vector-embedded — they live in Postgres with a
    full-text index (`idx_reviews_fts`, 'simple' config). So when a `focus` is
    given we rank this property's reviews by Postgres full-text relevance; without
    a focus (or no FTS match) we fall back to a balanced praise/complaint sample
    built on aspect polarity (see `build_polarity_sample_query`). All grounded in
    real rows.
    """
    if not listing_ids:
        return []

    pool = await get_pool()
    rows = []

    if focus and focus.strip():
        sql, params = build_focus_query(listing_ids, focus)
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)

    if not rows:
        sql, params = build_polarity_sample_query(listing_ids)
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)

    seen: set[str] = set()
    snippets: list[dict] = []
    for r in rows:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        snippets.append(_row_to_snippet(r))
    return snippets[:_MAX_SNIPPETS]


def _evidence_signal(snippet: dict) -> str:
    """Render the grounded per-review signal shown to the model.

    Replaces the old `rating {n/a}` — `reviews.rating` is always NULL, so that
    field carried no information at all.
    """
    bits: list[str] = []
    sentiment = snippet.get("sentiment")
    bits.append(
        f"sentiment {sentiment:+.2f}" if sentiment is not None else "sentiment n/a"
    )
    if snippet.get("praised"):
        bits.append("praises " + ", ".join(snippet["praised"]))
    if snippet.get("criticised"):
        bits.append("complaints " + ", ".join(snippet["criticised"]))
    if len(bits) == 1:
        bits.append("no aspect signal")
    return ", ".join(bits)


def _format_aspects(aspect_summary: dict[str, Any]) -> str:
    lines: list[str] = []
    for data in aspect_summary.values():
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
        evidence_lines.append(
            f"[{label}] ({_evidence_signal(s)}, lang {s['language']}): {s['text']}"
        )
    evidence = "\n".join(evidence_lines)

    focus_line = f"Focus the summary on: {focus}\n" if focus else ""
    prompt = (
        f"{focus_line}"
        "Synthesize these guest reviews. Cite the [r#] label after each claim.\n\n"
        f"REVIEWS:\n{evidence}\n"
        + (
            "\nAGGREGATE ASPECT SCORES (-1 to 1, higher=better; mean of the "
            f"per-review heuristic scores):\n{aspect_block}\n"
            if aspect_block
            else ""
        )
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


def _classify_snippet(snippet: dict) -> str:
    """Bucket one snippet: positive | negative | mixed | unscored.

    Sentiment (mean of the per-aspect scores) decides when present; thresholds
    are on the [-1, 1] scale the enrichment actually writes — the old >= 4.0 /
    <= 3.0 cut-offs were on a 1-5 star scale that no row ever carries. When
    sentiment is NULL (non-English review) we fall back to aspect polarity, which
    survives for the few keyword hits those rows do get.
    """
    sentiment = snippet.get("sentiment")
    praised = bool(snippet.get("praised"))
    criticised = bool(snippet.get("criticised"))
    if sentiment is not None:
        if sentiment >= _SENTIMENT_POS:
            return "positive"
        if sentiment <= _SENTIMENT_NEG:
            return "negative"
        return "mixed"
    if praised and criticised:
        return "mixed"
    if praised:
        return "positive"
    if criticised:
        return "negative"
    return "unscored"


def _top_themes(snippets: list[dict], key: str, limit: int = 3) -> list[str]:
    """Most frequent aspect names under `key` ('praised' | 'criticised')."""
    counts: dict[str, int] = {}
    for s in snippets:
        for name in s.get(key) or []:
            counts[name] = counts.get(name, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [name for name, _ in ranked[:limit]]


def _fallback_summary(snippets: list[dict], aspect_summary: dict) -> str:
    """Deterministic grounded summary used when the LLM is unavailable.

    Emits real `[r#]` labels (same 1-based order `synthesize` assigns), so the
    citation extraction downstream resolves them to the exact reviews cited
    instead of falling back to "cite everything".
    """
    if not snippets:
        return "No guest reviews were retrieved, so there is nothing to summarize."

    buckets: dict[str, list[dict]] = {
        "positive": [], "negative": [], "mixed": [], "unscored": [],
    }
    for i, s in enumerate(snippets, start=1):
        tagged = dict(s)
        tagged["_label"] = f"r{i}"
        buckets[_classify_snippet(tagged)].append(tagged)

    def cite(rows: list[dict]) -> str:
        return " ".join(f"[{r['_label']}]" for r in rows)

    def themes(rows: list[dict], key: str) -> str:
        names = _top_themes(rows, key)
        return f" mostly about {', '.join(names)}" if names else ""

    pos, neg = buckets["positive"], buckets["negative"]
    mixed, unscored = buckets["mixed"], buckets["unscored"]

    parts = [f"Based on {len(snippets)} retrieved review(s) (LLM unavailable):"]
    if pos:
        parts.append(f"{len(pos)} lean positive{themes(pos, 'praised')} {cite(pos)}.")
    if neg:
        parts.append(
            f"{len(neg)} raise concerns{themes(neg, 'criticised')} {cite(neg)}."
        )
    if mixed:
        parts.append(f"{len(mixed)} are mixed or near-neutral {cite(mixed)}.")
    if unscored and not (pos or neg or mixed):
        parts.append(
            f"{len(unscored)} carry no usable sentiment signal (typically "
            f"non-English reviews) {cite(unscored)}; read the cited snippets "
            "directly."
        )
    elif unscored:
        parts.append(f"{len(unscored)} carry no sentiment signal {cite(unscored)}.")
    if aspect_summary:
        parts.append("Aggregate aspect scores are available for this property.")
    return " ".join(parts)
