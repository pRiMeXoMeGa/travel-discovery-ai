"""Ingestion enrichments (brief §2.1 — at least two non-trivial ones).

Chosen enrichments (each unlocks UI/agent requirements):
  1. Aspect-level sentiment per review  → review topic filtering + aspect scores
  2. Per-property review summary        → "AI summary at top" + compare verdict
  3. Neighbourhood price percentile     → "is this expensive for the area"
  4. Amenity normalization              → consistent amenity filters

LLM cost note
-------------
Enrichments 1 and 2 are *toggleable*:
  - use_llm=False (default)  → heuristic fallback; fully offline; zero cost.
  - use_llm=True             → Gemini Flash free tier (60 req/min; 1 500 req/day).
    At 5 000 dev reviews that is ~4 LLM calls (batched 25 reviews/call) for
    aspect sentiment and ~1 000 listing summaries.  On the free tier that takes
    ~4 minutes for dev scale, ~2–3 hours for full 200 K reviews.
    Approximate token cost on Gemini Flash paid tier (if you exceed free quota):
      aspect sentiment: ~200 in + ~80 out per call × 200 batches ≈ $0.01
      summaries: ~500 in + ~200 out × 50 K ≈ $3–5 total for prod scale.

All functions accept an optional `conn` (asyncpg connection / pool) where they
need DB access.  SQL-only enrichments are synchronous and accept a psycopg2-style
connection with execute() for simplicity; the caller wraps them in a thread if
needed, or uses asyncpg's run_sync.
"""
from __future__ import annotations

import re
from typing import Optional

# ---------------------------------------------------------------------------
# Canonical amenity vocabulary and normalization map
# ---------------------------------------------------------------------------

# This must stay in sync with generate.py:CANONICAL_AMENITIES.
CANONICAL_AMENITIES: list[str] = [
    "wifi",
    "pool",
    "kitchen",
    "parking",
    "balcony",
    "ac",
    "gym",
    "washer",
    "pets_allowed",
    "hot_tub",
    "bbq",
    "workspace",
    "beach_access",
    "concierge",
    "breakfast_included",
    "ev_charger",
    "elevator",
    "baby_cot",
]

# Map of raw / alias strings → canonical name.
# Keys are lower-cased and stripped; values are canonical.
# Includes both generic aliases AND real Inside Airbnb amenity strings so that
# json.loads(listings.amenities) values are correctly mapped after normalization.
_AMENITY_MAP: dict[str, str] = {
    # ── wifi ─────────────────────────────────────────────────────────────────
    "wifi": "wifi",
    "wi-fi": "wifi",
    "wi fi": "wifi",
    "wireless": "wifi",
    "wireless internet": "wifi",
    "internet": "wifi",
    "broadband": "wifi",
    # Real Airbnb strings
    "fast wifi – 100 mbps": "wifi",
    "fast wifi – 200 mbps": "wifi",
    "fast wifi – 500 mbps": "wifi",
    "fast wifi – 1 gbps": "wifi",
    "fast wifi": "wifi",
    "ethernet connection": "wifi",
    "pocket wifi": "wifi",

    # ── pool ─────────────────────────────────────────────────────────────────
    "pool": "pool",
    "swimming pool": "pool",
    "private pool": "pool",
    "outdoor pool": "pool",
    "indoor pool": "pool",
    # Real Airbnb strings
    "shared pool": "pool",
    "private outdoor pool": "pool",
    "private indoor pool": "pool",
    "rooftop pool": "pool",
    "lap pool": "pool",
    "infinity pool": "pool",
    "plunge pool": "pool",
    "saltwater pool": "pool",
    "pool with pool toys": "pool",

    # ── kitchen ──────────────────────────────────────────────────────────────
    "kitchen": "kitchen",
    "kitchenette": "kitchen",
    "full kitchen": "kitchen",
    "equipped kitchen": "kitchen",
    # Real Airbnb strings
    "cooking basics": "kitchen",
    "dishes and silverware": "kitchen",
    "refrigerator": "kitchen",
    "mini fridge": "kitchen",
    "microwave": "kitchen",
    "oven": "kitchen",
    "stove": "kitchen",
    "toaster": "kitchen",
    "coffee maker": "kitchen",
    "rice maker": "kitchen",
    "blender": "kitchen",
    "hot water kettle": "kitchen",
    "freezer": "kitchen",
    "dining table": "kitchen",
    "wine glasses": "kitchen",

    # ── parking ──────────────────────────────────────────────────────────────
    "parking": "parking",
    "free parking": "parking",
    "private parking": "parking",
    "garage": "parking",
    "car park": "parking",
    # Real Airbnb strings
    "free parking on premises": "parking",
    "free parking on street": "parking",
    "free street parking": "parking",
    "paid parking off premises": "parking",
    "paid parking on premises": "parking",
    "paid street parking off premises": "parking",
    "paid valet parking on premises": "parking",
    "private garage": "parking",
    "carport": "parking",

    # ── balcony ──────────────────────────────────────────────────────────────
    "balcony": "balcony",
    "terrace": "balcony",
    "patio": "balcony",
    "private balcony": "balcony",
    # Real Airbnb strings
    "patio or balcony": "balcony",
    "private patio or balcony": "balcony",
    "shared patio or balcony": "balcony",
    "outdoor furniture": "balcony",
    "outdoor dining area": "balcony",
    "rooftop terrace": "balcony",

    # ── ac ───────────────────────────────────────────────────────────────────
    "ac": "ac",
    "air conditioning": "ac",
    "air conditioner": "ac",
    "climate control": "ac",
    "a/c": "ac",
    # Real Airbnb strings
    "central air conditioning": "ac",
    "window ac unit": "ac",
    "portable air conditioning": "ac",
    "split-type ductless system": "ac",
    "ceiling fan": "ac",

    # ── gym ──────────────────────────────────────────────────────────────────
    "gym": "gym",
    "fitness center": "gym",
    "fitness centre": "gym",
    "workout room": "gym",
    "exercise room": "gym",
    # Real Airbnb strings
    "shared gym": "gym",
    "private gym": "gym",
    "gym – shared": "gym",
    "gym – private": "gym",
    "exercise equipment": "gym",
    "indoor exercise equipment": "gym",

    # ── washer ───────────────────────────────────────────────────────────────
    "washer": "washer",
    "washing machine": "washer",
    "laundry": "washer",
    "laundry machine": "washer",
    "dryer": "washer",
    "washer/dryer": "washer",
    # Real Airbnb strings
    "free washer – in unit": "washer",
    "free washer – in building": "washer",
    "washer – in unit": "washer",
    "washer – in building": "washer",
    "paid washer – in building": "washer",
    "dryer – in unit": "washer",
    "dryer – in building": "washer",
    "laundromat nearby": "washer",

    # ── pets_allowed ─────────────────────────────────────────────────────────
    "pets allowed": "pets_allowed",
    "pet friendly": "pets_allowed",
    "pets": "pets_allowed",
    "dogs allowed": "pets_allowed",
    "cats allowed": "pets_allowed",
    # Real Airbnb strings (Airbnb uses exactly "pets allowed")

    # ── hot_tub ──────────────────────────────────────────────────────────────
    "hot tub": "hot_tub",
    "jacuzzi": "hot_tub",
    "spa": "hot_tub",
    "whirlpool": "hot_tub",
    # Real Airbnb strings
    "private hot tub": "hot_tub",
    "shared hot tub": "hot_tub",
    "hot tub – shared": "hot_tub",
    "hot tub – private": "hot_tub",

    # ── bbq ──────────────────────────────────────────────────────────────────
    "bbq": "bbq",
    "barbecue": "bbq",
    "grill": "bbq",
    "outdoor grill": "bbq",
    # Real Airbnb strings
    "bbq grill": "bbq",
    "shared bbq grill": "bbq",
    "private bbq grill": "bbq",
    "bbq grill – shared": "bbq",
    "bbq grill – private": "bbq",

    # ── workspace ────────────────────────────────────────────────────────────
    "workspace": "workspace",
    "dedicated workspace": "workspace",
    "desk": "workspace",
    "office space": "workspace",
    "home office": "workspace",
    # Real Airbnb strings (Airbnb uses "dedicated workspace" exactly)

    # ── beach_access ─────────────────────────────────────────────────────────
    "beach access": "beach_access",
    "beachfront": "beach_access",
    "near beach": "beach_access",
    "beach view": "beach_access",
    # Real Airbnb strings
    "private beach access": "beach_access",
    "shared beach access": "beach_access",
    "beach essentials": "beach_access",

    # ── concierge ────────────────────────────────────────────────────────────
    "concierge": "concierge",
    "24h concierge": "concierge",
    "front desk": "concierge",
    "reception": "concierge",

    # ── breakfast_included ───────────────────────────────────────────────────
    "breakfast included": "breakfast_included",
    "breakfast": "breakfast_included",
    "complimentary breakfast": "breakfast_included",
    # Real Airbnb strings — Airbnb uses bare "breakfast"

    # ── ev_charger ───────────────────────────────────────────────────────────
    "ev charger": "ev_charger",
    "electric vehicle charger": "ev_charger",
    "ev charging": "ev_charger",
    "tesla charger": "ev_charger",
    # Real Airbnb strings (Airbnb uses "ev charger" exactly)

    # ── elevator ─────────────────────────────────────────────────────────────
    "elevator": "elevator",
    "lift": "elevator",
    # Real Airbnb strings — Airbnb uses "elevator" exactly

    # ── baby_cot ─────────────────────────────────────────────────────────────
    "baby cot": "baby_cot",
    "crib": "baby_cot",
    "baby bed": "baby_cot",
    "travel cot": "baby_cot",
    # Real Airbnb strings — Airbnb uses "crib" exactly
}

_CANONICAL_SET = set(CANONICAL_AMENITIES)


def normalize_amenities(raw: list[str]) -> list[str]:
    """Map free-form amenity strings to the canonical vocabulary.

    Unknown / unmappable values are silently dropped.  The result is sorted
    and deduplicated so it is stable across equivalent input orderings.

    Idempotent: passing already-canonical values is a no-op.
    """
    result: set[str] = set()
    for item in raw:
        key = item.strip().lower()
        if key in _CANONICAL_SET:
            result.add(key)
        elif key in _AMENITY_MAP:
            result.add(_AMENITY_MAP[key])
        # else: unknown — drop silently
    return sorted(result)


# ---------------------------------------------------------------------------
# Aspect-level sentiment — heuristic fallback (no LLM)
# ---------------------------------------------------------------------------

# Simple keyword dictionaries.  Score in [-1, 1] range; None if not mentioned.
_ASPECT_POS: dict[str, list[str]] = {
    "cleanliness": ["clean", "spotless", "tidy", "immaculate", "hygiene", "fresh", "pristine"],
    "location": ["location", "central", "convenient", "close to", "walking distance",
                 "well-located", "accessible"],
    "value": ["affordable", "cheap", "reasonable", "great price", "bargain", "great value",
              "good value", "worth the price", "well priced"],
    "staff": ["helpful", "friendly", "responsive", "attentive", "welcoming",
              "kind", "polite", "professional", "great host", "lovely host",
              "amazing host"],
    "noise": [],  # noise positives are rare — absence of noise is positive
}

_ASPECT_NEG: dict[str, list[str]] = {
    "cleanliness": ["dirty", "dusty", "stains", "messy", "unclean", "smell", "mould", "mold"],
    "location": ["remote", "far from", "difficult to reach", "bad area", "unsafe"],
    "value": ["overpriced", "expensive", "rip-off", "costly", "not worth", "too pricey"],
    "staff": ["unresponsive", "rude", "unhelpful", "slow to respond", "unprofessional",
              "bad host", "terrible host", "terrible staff", "poor host",
              "bad staff", "awful host"],
    "noise": ["noisy", "loud", "noise", "disturbance", "traffic", "construction",
              "neighbour", "neighbor"],
}

# Negative intensifiers that flip the sentiment of the following aspect mention.
# Applied by scanning a 5-word window before any positive keyword hit.
_NEGATION_WORDS = frozenset([
    "terrible", "awful", "horrible", "dreadful", "poor", "bad", "not", "no",
    "wasn't", "wasn't", "weren't", "didn't", "don't", "doesn't", "never",
    "disappointing", "disappointing", "worst",
])

# Positive absence: if "noise" is not mentioned → mild positive signal.
_NOISE_POSITIVE_WORDS = ["quiet", "peaceful", "silent", "calm", "tranquil"]


def _has_negation_before(text_tokens: list[str], kw_tokens: list[str], window: int = 5) -> bool:
    """Return True if a negation word appears within `window` tokens before the keyword.

    Tokens are stripped of trailing punctuation for comparison.
    """
    kw_len = len(kw_tokens)
    stripped = [t.rstrip(".,!?;:'\"") for t in text_tokens]
    for i in range(len(stripped)):
        if stripped[i : i + kw_len] == kw_tokens:
            start = max(0, i - window)
            prefix = stripped[start:i]
            if any(neg in prefix for neg in _NEGATION_WORDS):
                return True
    return False


def _kw_matches_word_boundary(text: str, kw: str) -> bool:
    """Return True if `kw` appears as a whole-word (or whole-phrase) match in text.

    Avoids 'responsive' matching inside 'unresponsive'.
    """
    pattern = r"(?<![a-z])" + re.escape(kw) + r"(?![a-z])"
    return bool(re.search(pattern, text))


def _heuristic_aspect_sentiment(text: str) -> dict:
    """Return {aspects: {cleanliness, location, value, staff, noise}, sentiment} where
    each aspect score is in [-1, 1] or None if not mentioned.

    Uses whole-word matching for positive keywords and a negation window so phrases
    like 'terrible staff' or 'not helpful' correctly score negative.
    """
    lower = text.lower()
    tokens = lower.split()
    result: dict[str, Optional[float]] = {}

    for aspect in ["cleanliness", "location", "value", "staff"]:
        pos = 0
        neg = 0
        for kw in _ASPECT_POS[aspect]:
            if _kw_matches_word_boundary(lower, kw):
                kw_toks = kw.split()
                if _has_negation_before(tokens, kw_toks):
                    neg += 1   # negated positive → counts as negative
                else:
                    pos += 1
        for kw in _ASPECT_NEG[aspect]:
            if _kw_matches_word_boundary(lower, kw):
                neg += 1

        if pos == 0 and neg == 0:
            result[aspect] = None
        else:
            total = pos + neg
            result[aspect] = round((pos - neg) / total, 3)

    # Noise: negative keywords → negative score; positive words → positive.
    noise_neg = sum(1 for kw in _ASPECT_NEG["noise"] if kw in lower)
    noise_pos = sum(1 for kw in _NOISE_POSITIVE_WORDS if kw in lower)
    if noise_neg == 0 and noise_pos == 0:
        result["noise"] = None
    else:
        total = noise_neg + noise_pos
        result["noise"] = round((noise_pos - noise_neg) / total, 3)

    # Overall sentiment: mean of non-None aspects, or None.
    scores = [v for v in result.values() if v is not None]
    overall = round(sum(scores) / len(scores), 3) if scores else None

    return {"aspects": result, "sentiment": overall}


async def aspect_sentiment(
    review_text: str,
    use_llm: bool = False,
    llm_client=None,
) -> dict:
    """Return per-aspect sentiment for a single review text.

    Output schema::

        {
          "aspects": {
            "cleanliness": float | None,   # in [-1, 1]
            "location":    float | None,
            "value":       float | None,
            "staff":       float | None,
            "noise":       float | None,
          },
          "sentiment": float | None        # mean of non-null aspects
        }

    Parameters
    ----------
    review_text : str
        Raw review text (any language; heuristic works best on English).
    use_llm : bool
        If True, attempt an LLM call (requires llm_client).
        Falls back to heuristic on any failure.
    llm_client : optional
        A callable async function(prompt: str) -> str. See ingest.py for how to
        wire up Gemini Flash.
    """
    if not use_llm or llm_client is None:
        return _heuristic_aspect_sentiment(review_text)

    prompt = (
        "Analyze the following accommodation review and score each of these aspects "
        "on a scale from -1.0 (very negative) to 1.0 (very positive), or null if "
        "the aspect is not mentioned.\n"
        "Aspects: cleanliness, location, value, staff, noise\n"
        "Respond ONLY with valid JSON matching this schema:\n"
        '{"cleanliness": <float|null>, "location": <float|null>, '
        '"value": <float|null>, "staff": <float|null>, "noise": <float|null>}\n\n'
        f"Review:\n{review_text[:1000]}"   # cap to 1 000 chars to control tokens
    )

    try:
        import json as _json
        raw = await llm_client(prompt)
        parsed = _json.loads(raw)
        aspects = {
            k: (float(v) if v is not None else None)
            for k, v in parsed.items()
            if k in ("cleanliness", "location", "value", "staff", "noise")
        }
        scores = [v for v in aspects.values() if v is not None]
        overall = round(sum(scores) / len(scores), 3) if scores else None
        return {"aspects": aspects, "sentiment": overall}
    except Exception:
        # Graceful fallback — never lose a review to an LLM error.
        return _heuristic_aspect_sentiment(review_text)


async def aspect_sentiment_batch(
    reviews: list[tuple[str, str]],   # list of (review_id, text)
    use_llm: bool = False,
    llm_client=None,
    batch_size: int = 25,
) -> dict[str, dict]:
    """Batch version: returns {review_id: sentiment_dict}.

    When use_llm=True packs up to batch_size reviews into a single prompt to
    amortise API latency (1 call per batch_size reviews).

    Heuristic mode processes synchronously — no LLM calls.
    """
    result: dict[str, dict] = {}

    if not use_llm or llm_client is None:
        for rid, text in reviews:
            result[rid] = _heuristic_aspect_sentiment(text)
        return result

    # LLM batched path.
    import json as _json

    for i in range(0, len(reviews), batch_size):
        chunk = reviews[i : i + batch_size]
        numbered = "\n\n".join(
            f"[{j}] {text[:600]}" for j, (_, text) in enumerate(chunk)
        )
        prompt = (
            "For each numbered review below, score these aspects: "
            "cleanliness, location, value, staff, noise on [-1.0, 1.0] or null.\n"
            "Respond ONLY with a JSON array (one object per review, same order):\n"
            '[{"cleanliness": ..., "location": ..., "value": ..., '
            '"staff": ..., "noise": ...}, ...]\n\n'
            f"{numbered}"
        )
        try:
            raw = await llm_client(prompt)
            parsed = _json.loads(raw)
            for j, (rid, text) in enumerate(chunk):
                aspects = {
                    k: (float(v) if v is not None else None)
                    for k, v in parsed[j].items()
                    if k in ("cleanliness", "location", "value", "staff", "noise")
                }
                scores = [v for v in aspects.values() if v is not None]
                overall = round(sum(scores) / len(scores), 3) if scores else None
                result[rid] = {"aspects": aspects, "sentiment": overall}
        except Exception:
            # Fall back to heuristic for the whole chunk.
            for rid, text in chunk:
                result[rid] = _heuristic_aspect_sentiment(text)

    return result


# ---------------------------------------------------------------------------
# Per-property review summary
# ---------------------------------------------------------------------------

def _heuristic_summary(listing_id: str, reviews: list[str]) -> dict:
    """Offline fallback: a templated summary from the first few reviews."""
    if not reviews:
        return {
            "summary": "No reviews yet.",
            "aspect_avg": {k: None for k in ("cleanliness", "location", "value", "staff", "noise")},
        }
    # Compute aspect averages from heuristic scores.
    all_aspects: dict[str, list[float]] = {k: [] for k in ("cleanliness", "location", "value", "staff", "noise")}
    for text in reviews:
        scores = _heuristic_aspect_sentiment(text)["aspects"]
        for asp, score in scores.items():
            if score is not None:
                all_aspects[asp].append(score)

    aspect_avg = {
        k: (round(sum(v) / len(v), 3) if v else None)
        for k, v in all_aspects.items()
    }

    # Build a brief text summary from the first two reviews (truncated).
    snippets = [r[:120].replace("\n", " ") for r in reviews[:2]]
    summary_text = (
        f"Guests highlight: {'; '.join(snippets)}."
        if snippets else "No review text available."
    )
    return {"summary": summary_text, "aspect_avg": aspect_avg}


async def summarize_property(
    listing_id: str,
    reviews: list[str],
    use_llm: bool = False,
    llm_client=None,
) -> dict:
    """Return {summary: str, aspect_avg: dict} for a property.

    Output schema::

        {
          "summary":    str,
          "aspect_avg": {
            "cleanliness": float | None,
            "location":    float | None,
            "value":       float | None,
            "staff":       float | None,
            "noise":       float | None,
          }
        }

    Samples up to 20 reviews when calling the LLM to keep token count
    manageable (~500 in + ~200 out per listing).

    Cost at full scale (50 K listings, LLM enabled):
        50 000 × ~700 tokens ≈ 35 M tokens total.
        Gemini Flash free tier: 1 500 req/day → ~33 days to summarise all.
        Gemini Flash paid: ~$1.75 at $0.05/M input + $0.20/M output.
    """
    if not use_llm or llm_client is None:
        return _heuristic_summary(listing_id, reviews)

    sample = reviews[:20]   # cap to 20 reviews per prompt
    joined = "\n---\n".join(f"{i+1}. {r[:300]}" for i, r in enumerate(sample))
    prompt = (
        "You are summarizing accommodation reviews. Write a 2-3 sentence summary "
        "that captures the main positives and negatives. Be factual and neutral.\n\n"
        f"Reviews:\n{joined}\n\n"
        "Also provide average sentiment scores for: cleanliness, location, value, "
        "staff, noise — each from -1.0 to 1.0 or null if not mentioned.\n"
        "Respond ONLY with JSON: "
        '{"summary": "...", "aspect_avg": {"cleanliness": ..., "location": ..., '
        '"value": ..., "staff": ..., "noise": ...}}'
    )

    try:
        import json as _json
        raw = await llm_client(prompt)
        parsed = _json.loads(raw)
        return {
            "summary": str(parsed.get("summary", "")),
            "aspect_avg": {
                k: (float(v) if v is not None else None)
                for k, v in parsed.get("aspect_avg", {}).items()
                if k in ("cleanliness", "location", "value", "staff", "noise")
            },
        }
    except Exception:
        return _heuristic_summary(listing_id, reviews)


# ---------------------------------------------------------------------------
# Neighbourhood price percentile — pure SQL, no LLM
# ---------------------------------------------------------------------------

async def neighbourhood_price_percentile(conn) -> int:
    """Compute each listing's price percentile within its neighbourhood.

    Uses a single SQL statement with percent_rank() OVER (PARTITION BY city,
    neighbourhood ORDER BY base_price).  Result is written back to the
    neighbourhood_price_pct column.

    Returns the number of rows updated.

    Idempotent: re-running overwrites with the same values (deterministic
    given unchanged data).
    """
    sql = """
        UPDATE listings AS t
        SET    neighbourhood_price_pct = sub.pct
        FROM (
            SELECT
                id,
                percent_rank() OVER (
                    PARTITION BY city, neighbourhood
                    ORDER BY base_price
                ) AS pct
            FROM listings
        ) sub
        WHERE t.id = sub.id
    """
    result = await conn.execute(sql)
    # asyncpg returns "UPDATE N" as a string.
    try:
        updated = int(result.split()[-1])
    except (IndexError, ValueError):
        updated = -1
    return updated
