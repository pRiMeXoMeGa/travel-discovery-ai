"""Re-runnable ingestion pipeline (brief §2.1).

Two source paths — select with --source:
  real-csv  (default) — loads real Inside Airbnb data from csvData/ for
                         Amsterdam, Lisbon, and Los Angeles.
  synthetic           — generates synthetic listings/reviews (legacy / testing).

End-to-end: schema → load/generate → enrich → embed → index.
Never loads the full corpus into memory: all stages stream/batch.

Stores
------
- Postgres   : all listings + all reviews (relational)
- Qdrant     : all listings + all reviews embedded @ 384-dim (cosine, int8)

Usage
-----
    # Dev scale — real CSVs, ~2K listings / ~10K reviews (default)
    python ingest.py --recreate-qdrant

    # Full scale — real CSVs, all ~50K listings / ~200K reviews
    python ingest.py --scale full --recreate-qdrant

    # Custom scale
    python ingest.py --n-listings 5000 --n-reviews 20000

    # Synthetic data (legacy)
    python ingest.py --source synthetic --n-listings 1000 --n-reviews 5000

    # Enable LLM enrichments (requires GEMINI_API_KEY)
    python ingest.py --use-llm

    # Export pg_dump + qdrant snapshot after ingestion
    python ingest.py --snapshot

Memory profile
--------------
Dev scale  (2 K listings / 10 K reviews): < 300 MB RAM.
Prod scale (50 K listings / 200 K reviews): < 1.2 GB RAM (streamed in 256-item batches).

Timing estimate (CPU only, no GPU)
-----------------------------------
Dev scale : < 10 min (including model download on first run).
Prod scale: 60–120 min for embedding 250 K texts on a modern laptop CPU.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import math
import os
import random
import sys
import time
from pathlib import Path
from statistics import median
from typing import Iterator

import asyncpg
from dotenv import load_dotenv
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    PayloadSchemaType,
    PointStruct,
    QuantizationConfig,
    ScalarQuantization,
    ScalarQuantizationConfig,
    ScalarType,
    VectorParams,
)
from tqdm import tqdm

from enrich import (
    aspect_sentiment_batch,
    neighbourhood_price_percentile,
    normalize_amenities,
    summarize_property,
)
from generate import GenConfig, generate_listings, generate_reviews

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv(Path(__file__).parent.parent / ".env")

DATABASE_URL: str = os.environ.get(
    "DATABASE_URL", "postgresql://travel:travel@localhost:5432/travel"
)
QDRANT_URL: str = os.environ.get("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY: str | None = os.environ.get("QDRANT_API_KEY") or None

COLLECTION_LISTINGS  = os.environ.get("QDRANT_COLLECTION_LISTINGS",  "listings")
COLLECTION_REVIEWS   = os.environ.get("QDRANT_COLLECTION_REVIEWS",   "reviews")
COLLECTION_SUMMARIES = os.environ.get("QDRANT_COLLECTION_SUMMARIES", "summaries")

EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
VECTOR_DIM = 384
DISTANCE = Distance.COSINE

# Batch sizes — tuned for the memory/throughput sweet-spot on a laptop.
# Increasing DB_BATCH reduces round-trips; keep ≤ 500 to avoid asyncpg param limits.
DB_BATCH_LISTINGS  = 256
DB_BATCH_REVIEWS   = 256
EMBED_BATCH_SIZE   = 256   # fastembed processes this many texts at a time in ONNX
QDRANT_UPSERT_BATCH = 256  # Qdrant HTTP payload size; 256 × 384 floats ≈ 400 KB

# Real Airbnb reviews are long paragraphs; embedding them at bge-small's full
# 512-token window is ~10x slower than short text and dominates the run
# (3,391s for 10K reviews in the dry run). We embed only the leading chars —
# the review's topic/sentiment is captured early — and keep the FULL text in
# Postgres for display + citations. This is the key throughput fix.
REVIEW_EMBED_CHARS = 320

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


async def apply_schema(conn: asyncpg.Connection) -> None:
    """Run schema.sql idempotently (all CREATE ... IF NOT EXISTS)."""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    await conn.execute(sql)
    print("[schema] applied.")


# ---------------------------------------------------------------------------
# Qdrant helpers
# ---------------------------------------------------------------------------

def _int8_quantization() -> QuantizationConfig:
    return ScalarQuantization(
        scalar=ScalarQuantizationConfig(
            type=ScalarType.INT8,
            always_ram=True,
        )
    )


async def ensure_collection(
    client: AsyncQdrantClient,
    name: str,
    recreate: bool = False,
) -> None:
    """Create a collection if it does not exist (idempotent unless recreate=True)."""
    existing = {c.name for c in (await client.get_collections()).collections}
    if name in existing:
        if recreate:
            await client.delete_collection(name)
        else:
            print(f"[qdrant] collection '{name}' already exists — skipping create.")
            return

    await client.create_collection(
        collection_name=name,
        vectors_config=VectorParams(size=VECTOR_DIM, distance=DISTANCE),
        quantization_config=_int8_quantization(),
    )
    print(f"[qdrant] created collection '{name}' (dim={VECTOR_DIM}, cosine, int8).")


# Payload fields the retrieval/itinerary agents filter on (see
# backend/app/agents/retrieval.py). Qdrant Cloud runs in STRICT mode and 400s on
# filtering an un-indexed field ("Index required but not found"); local Qdrant
# allows it via full scan. So these indexes are MANDATORY for cloud and carried
# inside the snapshot, so a restored cluster works without manual fixup.
_LISTINGS_PAYLOAD_INDEXES = {
    "city": PayloadSchemaType.KEYWORD,
    "type": PayloadSchemaType.KEYWORD,
    "neighbourhood": PayloadSchemaType.KEYWORD,
    "amenities": PayloadSchemaType.KEYWORD,
    "base_price": PayloadSchemaType.FLOAT,
    "beds": PayloadSchemaType.INTEGER,
}


async def ensure_payload_indexes(client: AsyncQdrantClient, name: str) -> None:
    """Create the listings payload indexes (idempotent; safe to re-run)."""
    for field, schema in _LISTINGS_PAYLOAD_INDEXES.items():
        try:
            await client.create_payload_index(
                collection_name=name, field_name=field, field_schema=schema
            )
        except Exception as exc:  # noqa: BLE001 — index may already exist
            print(f"[qdrant] payload index '{field}' skipped: {exc}")
    print(f"[qdrant] payload indexes ensured on '{name}'.")


# ---------------------------------------------------------------------------
# fastembed — lazy singleton so the model loads once
# ---------------------------------------------------------------------------

_embed_model = None


def get_embed_model():
    global _embed_model
    if _embed_model is None:
        from fastembed import TextEmbedding
        print(f"[embed] loading model '{EMBEDDING_MODEL}' (first run downloads ~23 MB)…")
        # threads=cpu_count: benchmark showed ~1.6x over the default on this 4-core box.
        _embed_model = TextEmbedding(model_name=EMBEDDING_MODEL, threads=os.cpu_count())
        print("[embed] model ready.")
    return _embed_model


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts; return list of 384-dim vectors."""
    model = get_embed_model()
    # fastembed returns a generator of numpy arrays.
    return [vec.tolist() for vec in model.embed(texts)]


# ---------------------------------------------------------------------------
# Postgres insert helpers (idempotent via ON CONFLICT DO NOTHING)
# ---------------------------------------------------------------------------

_INSERT_LISTING = """
    INSERT INTO listings
        (id, name, type, city, neighbourhood, lat, lng, base_price, beds,
         amenities, photos, host, rating, review_count, neighbourhood_price_pct)
    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
    ON CONFLICT (id) DO NOTHING
"""

_INSERT_REVIEW = """
    INSERT INTO reviews
        (id, listing_id, date, reviewer, rating, text, language, aspects, sentiment)
    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
    ON CONFLICT (id) DO NOTHING
"""


def _listing_to_row(l: dict) -> tuple:
    return (
        l["id"],
        l["name"],
        l["type"],
        l["city"],
        l["neighbourhood"],
        l["lat"],
        l["lng"],
        l["base_price"],
        l["beds"],
        json.dumps(l["amenities"]),
        json.dumps(l["photos"]),
        json.dumps(l["host"]),
        l["rating"],
        l["review_count"],
        l["neighbourhood_price_pct"],
    )


def _review_to_row(r: dict) -> tuple:
    from datetime import date as _date
    d = r["date"]
    if d is None:
        pass  # Postgres DATE column accepts None → NULL
    elif isinstance(d, str):
        try:
            d = _date.fromisoformat(d)
        except ValueError:
            d = None
    return (
        r["id"],
        r["listing_id"],
        d,
        r["reviewer"],
        r["rating"],
        r["text"],
        r["language"],
        json.dumps(r["aspects"]) if r["aspects"] is not None else None,
        r["sentiment"],
    )


async def insert_listings_batch(
    pool: asyncpg.Pool,
    batch: list[dict],
) -> None:
    rows = [_listing_to_row(l) for l in batch]
    async with pool.acquire() as conn:
        await conn.executemany(_INSERT_LISTING, rows)


async def insert_reviews_batch(
    pool: asyncpg.Pool,
    batch: list[dict],
) -> None:
    rows = [_review_to_row(r) for r in batch]
    async with pool.acquire() as conn:
        await conn.executemany(_INSERT_REVIEW, rows)


# ---------------------------------------------------------------------------
# Qdrant upsert helpers
# ---------------------------------------------------------------------------

def _listing_to_point(listing: dict, vector: list[float]) -> PointStruct:
    """Convert a listing dict + vector to a Qdrant PointStruct.

    Point ID: deterministic integer derived from the UUID so Qdrant can accept
    an int64 ID (Qdrant supports both UUID strings and uint64 ints; we use
    the first 8 bytes of the UUID as an int for compact storage).
    """
    import uuid as _uuid
    uid = _uuid.UUID(listing["id"])
    point_id = uid.int >> 64   # top 64 bits; always positive
    return PointStruct(
        id=point_id,
        vector=vector,
        payload={
            "listing_id": listing["id"],
            "name": listing["name"],
            "type": listing["type"],
            "city": listing["city"],
            "neighbourhood": listing["neighbourhood"],
            "lat": listing["lat"],
            "lng": listing["lng"],
            "base_price": float(listing["base_price"]),
            "beds": listing["beds"],
            "rating": listing["rating"],
            "amenities": listing["amenities"],
        },
    )


def _review_to_point(review: dict, vector: list[float]) -> PointStruct:
    import uuid as _uuid
    uid = _uuid.UUID(review["id"])
    point_id = uid.int >> 64
    return PointStruct(
        id=point_id,
        vector=vector,
        payload={
            "review_id":  review["id"],
            "listing_id": review["listing_id"],
            "rating":     review["rating"],
            "language":   review["language"],
            "date":       review["date"],
            "sentiment":  review.get("sentiment"),
            # Aspect scores stored flat for Qdrant payload filtering.
            **(
                {f"asp_{k}": v for k, v in (review.get("aspects") or {}).items()}
                if review.get("aspects") else {}
            ),
        },
    )


async def upsert_points_batch(
    client: AsyncQdrantClient,
    collection: str,
    points: list[PointStruct],
) -> None:
    await client.upsert(collection_name=collection, points=points, wait=True)


# ---------------------------------------------------------------------------
# LLM client factory (optional)
# ---------------------------------------------------------------------------

async def _make_llm_client():
    """Return an async callable (prompt: str) -> str, or None if no key."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return None
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        model_name = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
        model = genai.GenerativeModel(model_name)

        # Stay under the free-tier RPM and retry on rate-limit (429 /
        # ResourceExhausted) with exponential backoff, so calls are NOT silently
        # dropped to the heuristic fallback. Tunable via env.
        min_interval = float(os.environ.get("LLM_MIN_INTERVAL_S", "6.5"))  # ~9 req/min
        max_retries = int(os.environ.get("LLM_MAX_RETRIES", "5"))
        _last = {"t": 0.0}
        _lock = asyncio.Lock()

        async def _call(prompt: str) -> str:
            async with _lock:  # serialize + throttle across all callers
                wait = min_interval - (time.monotonic() - _last["t"])
                if wait > 0:
                    await asyncio.sleep(wait)
                attempt = 0
                while True:
                    try:
                        response = await asyncio.to_thread(model.generate_content, prompt)
                        _last["t"] = time.monotonic()
                        return response.text
                    except Exception as exc:
                        msg = str(exc).lower()
                        rate_limited = any(
                            s in msg for s in ("429", "resourceexhausted", "quota", "rate limit")
                        )
                        attempt += 1
                        if attempt > max_retries or not rate_limited:
                            raise
                        backoff = min(60.0, 2.0 * (2 ** attempt))
                        print(f"[llm] rate-limited; backing off {backoff:.0f}s (retry {attempt}/{max_retries})")
                        await asyncio.sleep(backoff)
                        _last["t"] = time.monotonic()

        return _call
    except Exception as exc:
        print(f"[llm] failed to init Gemini ({exc}); LLM enrichments disabled.")
        return None


# ---------------------------------------------------------------------------
# Real Inside Airbnb CSV loader
# ---------------------------------------------------------------------------

# City folder name → canonical city string stored in listings.city
_CITY_FOLDER_MAP: dict[str, str] = {
    "amsterdam":   "Amsterdam",
    "lisbon":      "Lisbon",
    "los angeles": "Los Angeles",
}

# Full-scale listing quotas per city (for --scale full)
_CITY_FULL_QUOTAS: dict[str, int] = {
    "Amsterdam":   10_480,
    "Lisbon":      19_760,
    "Los Angeles": 19_760,
}

# Dev-scale listing quotas (~660/city → total ~2,000)
_CITY_DEV_QUOTAS: dict[str, int] = {
    "Amsterdam":   660,
    "Lisbon":      670,
    "Los Angeles": 670,
}


def _stable_listing_id(raw_id: str) -> str:
    """Convert a raw Airbnb integer listing ID to a stable UUID string.

    We namespace the raw integer under a fixed UUID v5 namespace so the IDs
    are compact, stable, and guaranteed unique across cities even if two cities
    ever share an integer ID.
    """
    import uuid as _uuid
    ns = _uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # URL namespace
    return str(_uuid.uuid5(ns, f"airbnb:listing:{raw_id}"))


def _stable_review_id(raw_id: str) -> str:
    """Convert a raw Airbnb review integer ID to a stable UUID string."""
    import uuid as _uuid
    ns = _uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
    return str(_uuid.uuid5(ns, f"airbnb:review:{raw_id}"))


def _clean_price(price_str: str) -> float | None:
    """Strip $ / € / commas and parse to float; return None on failure."""
    if not price_str:
        return None
    cleaned = price_str.strip().lstrip("$€£").replace(",", "")
    try:
        v = float(cleaned)
        return v if v > 0 else None
    except ValueError:
        return None


def _parse_beds(row: dict) -> int:
    """Resolve beds from real CSV columns with fallback chain per contract."""
    # 1. beds column
    try:
        v = int(float(row.get("beds", "") or 0))
        if v > 0:
            return v
    except (ValueError, TypeError):
        pass
    # 2. bedrooms column
    try:
        v = int(float(row.get("bedrooms", "") or 0))
        if v > 0:
            return v
    except (ValueError, TypeError):
        pass
    # 3. ceil(accommodates / 2), min 1
    try:
        acc = int(float(row.get("accommodates", "") or 2))
        return max(1, math.ceil(acc / 2))
    except (ValueError, TypeError):
        return 1


def _parse_rating(row: dict) -> float | None:
    """Parse review_scores_rating; divide by 20 if > 5 (100-point scale)."""
    raw = row.get("review_scores_rating", "").strip()
    if not raw:
        return None
    try:
        v = float(raw)
        if v > 5:
            v = v / 20.0
        return round(max(0.0, min(5.0, v)), 2)
    except ValueError:
        return None


def _parse_amenities(row: dict) -> list[str]:
    """Parse JSON array of amenity strings from the CSV amenities column."""
    raw = row.get("amenities", "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(a) for a in parsed]
    except (json.JSONDecodeError, ValueError):
        pass
    return []


def _detect_language(text: str) -> str | None:
    """Detect language of review text; cap at 500 chars for speed.

    Returns ISO 639-1 code or None on failure/short text.
    """
    if not text or len(text.strip()) < 10:
        return None
    try:
        from langdetect import detect, LangDetectException
        return detect(text[:500])
    except Exception:
        return None


class _CityPhotoPool:
    """Deterministic per-city photo pool built from real picture_url values.

    Built once per city from all listings' picture_url fields.  Each listing
    gets a deterministic slice of ≥4 photos:
      slot 0 = own picture_url (or pool fallback if empty)
      slots 1-5 = other urls chosen by hash(listing_id) mod pool_size
    """

    def __init__(self) -> None:
        self._pools: dict[str, list[str]] = {}

    def add(self, city: str, url: str) -> None:
        if url and url.strip():
            self._pools.setdefault(city, []).append(url.strip())

    def get_photos(self, city: str, listing_id: str, hero_url: str) -> list[str]:
        pool = self._pools.get(city, [])
        if not pool:
            return [hero_url] if hero_url else []

        # Deterministic index into pool via hash.
        h = int(hashlib.md5(listing_id.encode()).hexdigest(), 16)
        target_n = 5  # aim for 5 photos; min contract is 4

        photos: list[str] = []
        # Slot 0: hero
        if hero_url and hero_url.strip():
            photos.append(hero_url.strip())

        # Fill remaining slots from pool, skipping hero to avoid duplicates.
        pool_size = len(pool)
        idx = h % pool_size
        added = 0
        for offset in range(pool_size):
            if len(photos) >= target_n:
                break
            candidate = pool[(idx + offset) % pool_size]
            if candidate not in photos:
                photos.append(candidate)
                added += 1

        return photos


def _stream_listings_csv(
    city_folder: str,
    city_name: str,
    quota: int,
    photo_pool: _CityPhotoPool,
    seed: int = 42,
) -> Iterator[dict]:
    """Stream listing dicts from a city's listings.csv.

    Selection: top-`quota` rows by number_of_reviews DESC (most-reviewed first),
    then deterministic seeded sample among ties so the selection is stable
    across re-runs with the same seed.

    Memory: reads the file once to sort IDs (O(n_listings) integers), then
    yields one row at a time via a second scan.  At 20K listings the ID list
    is ~160 KB — negligible.
    """
    csv_path = Path(__file__).parent.parent / "csvData" / city_folder / "listings.csv"
    if not csv_path.exists():
        raise FileNotFoundError(f"listings.csv not found: {csv_path}")

    # --- Pass 1: collect (review_count, id) pairs for ranking ---
    id_rank: list[tuple[int, str]] = []   # (n_reviews_desc, raw_id)
    with open(csv_path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            raw_id = row.get("id", "").strip()
            if not raw_id:
                continue
            try:
                n_rev = int(float(row.get("number_of_reviews", "0") or 0))
            except ValueError:
                n_rev = 0
            id_rank.append((n_rev, raw_id))

    # Sort descending by review count; within ties use seeded random for
    # deterministic ordering (not alphabetical, which would bias toward low IDs).
    rng = random.Random(seed)
    id_rank.sort(key=lambda x: (-x[0], rng.random()))

    selected_raw_ids: set[str] = {raw_id for _, raw_id in id_rank[:quota]}

    # --- Pass 2: build price map for median imputation ---
    # We need city+room_type medians for price imputation, but this requires
    # knowing prices for selected listings first.  We do a targeted scan.
    price_by_type: dict[str, list[float]] = {}
    with open(csv_path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("id", "").strip() not in selected_raw_ids:
                continue
            rt = row.get("room_type", "").strip()
            price = _clean_price(row.get("price", ""))
            if price is not None:
                price_by_type.setdefault(rt, []).append(price)

    median_price: dict[str, float] = {
        rt: median(prices) for rt, prices in price_by_type.items() if prices
    }
    global_median = median(
        [p for prices in price_by_type.values() for p in prices]
    ) if price_by_type else 100.0

    # --- Pass 3: yield mapped listing dicts ---
    with open(csv_path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            raw_id = row.get("id", "").strip()
            if raw_id not in selected_raw_ids:
                continue

            listing_id = _stable_listing_id(raw_id)

            # Neighbourhood: prefer neighbourhood_cleansed, fallback neighbourhood.
            neighbourhood = (
                row.get("neighbourhood_cleansed", "").strip()
                or row.get("neighbourhood", "").strip()
                or "Unknown"
            )

            # Coordinates.
            try:
                lat = float(row.get("latitude", "0") or 0)
                lng = float(row.get("longitude", "0") or 0)
            except ValueError:
                lat, lng = 0.0, 0.0

            # Price with median imputation.
            room_type = row.get("room_type", "").strip()
            base_price = _clean_price(row.get("price", ""))
            if base_price is None:
                base_price = median_price.get(room_type, global_median)

            # Beds.
            beds = _parse_beds(row)

            # Amenities: parse JSON array → normalize to 18-term vocab.
            raw_amenities = _parse_amenities(row)
            amenities = normalize_amenities(raw_amenities)

            # Photos: build from hero + city pool.
            hero_url = row.get("picture_url", "").strip()
            # Register hero for pool (pool may not be built yet on first city pass,
            # but _CityPhotoPool.get_photos only uses what was added before this call).
            photos = photo_pool.get_photos(city_name, listing_id, hero_url)
            # Guarantee ≥4 photos — if pool is sparse, pad with hero duplicated
            # (unusual; only happens if city has < 4 total picture_urls).
            while len(photos) < 4 and hero_url:
                photos.append(hero_url)

            # Host.
            host = {
                "id": row.get("host_id", "").strip(),
                "name": row.get("host_name", "").strip(),
                "superhost": row.get("host_is_superhost", "").strip().lower() == "t",
            }

            # Rating.
            rating = _parse_rating(row)

            # Review count.
            try:
                review_count = int(float(row.get("number_of_reviews", "0") or 0))
            except ValueError:
                review_count = 0

            yield {
                "id": listing_id,
                "name": row.get("name", "").strip() or f"Listing {raw_id}",
                "type": room_type,
                "city": city_name,
                "neighbourhood": neighbourhood,
                "lat": lat,
                "lng": lng,
                "base_price": round(base_price, 2),
                "beds": beds,
                "amenities": amenities,
                "photos": photos,
                "host": host,
                "rating": rating,
                "review_count": review_count,
                "neighbourhood_price_pct": None,
                # Keep raw_id for review join.
                "_raw_id": raw_id,
            }


def _stream_reviews_csv(
    city_folder: str,
    listing_id_map: dict[str, str],  # raw_id → stable_uuid
    quota: int,
    seed: int = 42,
) -> Iterator[dict]:
    """Stream review dicts from a city's reviews.csv.

    Selects up to `quota` reviews from listings present in listing_id_map.
    Selection: seeded uniform sample per listing, capped to keep total ≤ quota.
    Language detected via langdetect from real comment text.

    Memory: builds a {raw_listing_id: [review_rows]} index in-memory only for
    selected listings.  At 3,300 reviews × ~300 chars avg ≈ ~1 MB — fine.
    At full scale (66K reviews/city) budget is ~20 MB per city — acceptable.
    """
    csv_path = Path(__file__).parent.parent / "csvData" / city_folder / "reviews.csv"
    if not csv_path.exists():
        print(f"[warn] reviews.csv not found for {city_folder} — skipping reviews.")
        return

    selected_listing_ids = set(listing_id_map.keys())

    # Collect all review rows for selected listings.
    reviews_by_listing: dict[str, list[dict]] = {}
    with open(csv_path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            lid = row.get("listing_id", "").strip()
            if lid not in selected_listing_ids:
                continue
            text = row.get("comments", "").strip()
            if not text:
                continue
            reviews_by_listing.setdefault(lid, []).append(row)

    # Seeded sample: distribute quota proportionally across listings.
    rng = random.Random(seed + 7)
    # Shuffle each listing's reviews deterministically.
    for lid in reviews_by_listing:
        rng.shuffle(reviews_by_listing[lid])

    # Interleave: round-robin across listings (evens out distribution).
    listing_order = sorted(reviews_by_listing.keys())  # stable sort
    rng.shuffle(listing_order)  # then seeded shuffle

    pointers = {lid: 0 for lid in listing_order}
    emitted = 0

    while emitted < quota:
        advanced = False
        for lid in listing_order:
            if emitted >= quota:
                break
            rows = reviews_by_listing.get(lid, [])
            ptr = pointers[lid]
            if ptr >= len(rows):
                continue
            row = rows[ptr]
            pointers[lid] = ptr + 1
            advanced = True

            raw_review_id = row.get("id", "").strip()
            review_id = _stable_review_id(raw_review_id) if raw_review_id else _stable_review_id(f"{lid}:{ptr}")
            stable_listing_id = listing_id_map[lid]

            text = row.get("comments", "").strip()
            language = _detect_language(text)

            date_str = row.get("date", "").strip() or None

            yield {
                "id": review_id,
                "listing_id": stable_listing_id,
                "date": date_str,
                "reviewer": row.get("reviewer_name", "").strip() or None,
                "rating": None,   # reviews.csv has no per-review star rating
                "text": text,
                "language": language,
                "aspects": None,  # filled by aspect_sentiment enrichment
                "sentiment": None,
            }
            emitted += 1

        if not advanced:
            break  # exhausted all reviews for selected listings


# ---------------------------------------------------------------------------
# Stage: load real CSVs → Postgres
# ---------------------------------------------------------------------------

async def stage_real_csv_listings(
    pool: asyncpg.Pool,
    city_folders: list[str],
    quotas: dict[str, int],
    seed: int = 42,
) -> dict[str, dict[str, str]]:
    """Load real Airbnb listings from CSVs into Postgres.

    Builds a two-pass per-city photo pool: first pass collects all picture_urls
    for selected listings, second pass assigns photos using the full pool.

    Returns: {city_folder: {raw_id: stable_uuid}} for review join.
    Memory: O(selected_listing_count) for photo pool + ID map.
    """
    print("\n[listings] loading from real Inside Airbnb CSVs…")
    t0 = time.time()
    total_inserted = 0
    city_id_maps: dict[str, dict[str, str]] = {}

    for city_folder in city_folders:
        city_name = _CITY_FOLDER_MAP[city_folder]
        quota = quotas.get(city_name, 0)
        print(f"  [listings] {city_name}: quota={quota:,}")

        # --- Phase A: build photo pool for this city ---
        # We need a single pre-pass to collect all picture_urls so the pool is
        # available when we actually stream listing rows.
        photo_pool = _CityPhotoPool()
        csv_path = Path(__file__).parent.parent / "csvData" / city_folder / "listings.csv"

        # Collect raw IDs for the quota (same ranking logic as _stream_listings_csv).
        id_rank: list[tuple[int, str]] = []
        with open(csv_path, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                raw_id = row.get("id", "").strip()
                if not raw_id:
                    continue
                try:
                    n_rev = int(float(row.get("number_of_reviews", "0") or 0))
                except ValueError:
                    n_rev = 0
                id_rank.append((n_rev, raw_id))

        rng = random.Random(seed)
        id_rank.sort(key=lambda x: (-x[0], rng.random()))
        selected_raw_ids: set[str] = {raw_id for _, raw_id in id_rank[:quota]}

        # Build photo pool from selected listings' picture_urls.
        with open(csv_path, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                raw_id = row.get("id", "").strip()
                if raw_id not in selected_raw_ids:
                    continue
                url = row.get("picture_url", "").strip()
                if url:
                    photo_pool.add(city_name, url)

        # --- Phase B: stream + insert listings ---
        batch: list[dict] = []
        raw_id_map: dict[str, str] = {}  # raw_id → stable_uuid
        city_count = 0

        for listing in tqdm(
            _stream_listings_csv(city_folder, city_name, quota, photo_pool, seed),
            total=quota,
            desc=f"  {city_name}",
            unit="listing",
        ):
            raw_id = listing.pop("_raw_id")
            raw_id_map[raw_id] = listing["id"]
            batch.append(listing)
            city_count += 1

            if len(batch) >= DB_BATCH_LISTINGS:
                await insert_listings_batch(pool, batch)
                total_inserted += len(batch)
                batch = []

        if batch:
            await insert_listings_batch(pool, batch)
            total_inserted += len(batch)

        city_id_maps[city_folder] = raw_id_map
        print(f"  [listings] {city_name}: {city_count} rows loaded.")

    elapsed = time.time() - t0
    print(f"[listings] total {total_inserted} rows in {elapsed:.1f}s.")
    return city_id_maps


async def stage_real_csv_reviews(
    pool: asyncpg.Pool,
    city_folders: list[str],
    city_id_maps: dict[str, dict[str, str]],
    review_quotas: dict[str, int],
    seed: int = 42,
    use_llm: bool = False,
    llm_client=None,
) -> None:
    """Load real Airbnb reviews from CSVs, detect language, enrich, insert.

    Memory: O(review_quota/city × avg_row_size) — at 3,300 reviews × 300 chars
    that is ~1 MB per city batch pass.
    """
    print("\n[reviews] loading from real Inside Airbnb CSVs…")
    t0 = time.time()
    total_inserted = 0

    for city_folder in city_folders:
        city_name = _CITY_FOLDER_MAP[city_folder]
        listing_id_map = city_id_maps.get(city_folder, {})
        if not listing_id_map:
            print(f"  [reviews] {city_name}: no listings loaded — skipping.")
            continue

        quota = review_quotas.get(city_name, 0)
        print(f"  [reviews] {city_name}: quota={quota:,}, listing pool={len(listing_id_map):,}")

        batch: list[dict] = []
        city_count = 0

        for review in tqdm(
            _stream_reviews_csv(city_folder, listing_id_map, quota, seed),
            total=quota,
            desc=f"  {city_name}",
            unit="review",
        ):
            batch.append(review)
            if len(batch) >= DB_BATCH_REVIEWS:
                # Aspect sentiment enrichment on batch.
                pairs = [(r["id"], r["text"]) for r in batch]
                sentiments = await aspect_sentiment_batch(
                    pairs, use_llm=use_llm, llm_client=llm_client
                )
                for r in batch:
                    info = sentiments.get(r["id"], {"aspects": None, "sentiment": None})
                    r["aspects"] = info.get("aspects")
                    r["sentiment"] = info.get("sentiment")

                await insert_reviews_batch(pool, batch)
                total_inserted += len(batch)
                city_count += len(batch)
                batch = []

        if batch:
            pairs = [(r["id"], r["text"]) for r in batch]
            sentiments = await aspect_sentiment_batch(
                pairs, use_llm=use_llm, llm_client=llm_client
            )
            for r in batch:
                info = sentiments.get(r["id"], {"aspects": None, "sentiment": None})
                r["aspects"] = info.get("aspects")
                r["sentiment"] = info.get("sentiment")
            await insert_reviews_batch(pool, batch)
            total_inserted += len(batch)
            city_count += len(batch)

        print(f"  [reviews] {city_name}: {city_count} rows loaded.")

    elapsed = time.time() - t0
    print(f"[reviews] total {total_inserted} rows in {elapsed:.1f}s.")


# ---------------------------------------------------------------------------
# Stage: generate + insert listings (synthetic — legacy)
# ---------------------------------------------------------------------------

async def stage_listings(
    pool: asyncpg.Pool,
    cfg: GenConfig,
) -> list[str]:
    """Generate listings, normalise amenities, insert to Postgres.

    Returns the full ordered list of listing IDs (held in memory — at 50 K
    listings this is ~50 K × ~36 bytes ≈ 1.8 MB, well within budget).
    """
    print(f"\n[listings] generating {cfg.n_listings} listings…")
    listing_ids: list[str] = []
    batch: list[dict] = []
    inserted = 0
    t0 = time.time()

    for listing in tqdm(generate_listings(cfg), total=cfg.n_listings, unit="listing"):
        # Amenity normalisation (already canonical from generator, but apply
        # idempotently so re-runs from real CSV data are safe).
        listing["amenities"] = normalize_amenities(listing["amenities"])
        listing_ids.append(listing["id"])
        batch.append(listing)

        if len(batch) >= DB_BATCH_LISTINGS:
            await insert_listings_batch(pool, batch)
            inserted += len(batch)
            batch = []

    if batch:
        await insert_listings_batch(pool, batch)
        inserted += len(batch)

    elapsed = time.time() - t0
    print(f"[listings] inserted {inserted} rows in {elapsed:.1f}s.")
    return listing_ids


# ---------------------------------------------------------------------------
# Stage: generate + insert reviews (with heuristic aspect sentiment)
# ---------------------------------------------------------------------------

async def stage_reviews(
    pool: asyncpg.Pool,
    cfg: GenConfig,
    listing_ids: list[str],
    use_llm: bool = False,
    llm_client=None,
) -> None:
    """Generate reviews, run aspect sentiment enrichment, insert to Postgres."""
    print(f"\n[reviews] generating {cfg.n_reviews} reviews…")
    batch: list[dict] = []
    inserted = 0
    t0 = time.time()

    for review in tqdm(
        generate_reviews(cfg, listing_ids),
        total=cfg.n_reviews,
        unit="review",
    ):
        batch.append(review)

        if len(batch) >= DB_BATCH_REVIEWS:
            # Run aspect sentiment on the whole batch at once.
            pairs = [(r["id"], r["text"]) for r in batch]
            sentiments = await aspect_sentiment_batch(
                pairs, use_llm=use_llm, llm_client=llm_client
            )
            for r in batch:
                info = sentiments.get(r["id"], {"aspects": None, "sentiment": None})
                r["aspects"] = info.get("aspects")
                r["sentiment"] = info.get("sentiment")

            await insert_reviews_batch(pool, batch)
            inserted += len(batch)
            batch = []

    if batch:
        pairs = [(r["id"], r["text"]) for r in batch]
        sentiments = await aspect_sentiment_batch(
            pairs, use_llm=use_llm, llm_client=llm_client
        )
        for r in batch:
            info = sentiments.get(r["id"], {"aspects": None, "sentiment": None})
            r["aspects"] = info.get("aspects")
            r["sentiment"] = info.get("sentiment")
        await insert_reviews_batch(pool, batch)
        inserted += len(batch)

    elapsed = time.time() - t0
    print(f"[reviews] inserted {inserted} rows in {elapsed:.1f}s.")


# ---------------------------------------------------------------------------
# Stage: embed listings → Qdrant
# ---------------------------------------------------------------------------

async def stage_embed_listings(
    pool: asyncpg.Pool,
    qdrant: AsyncQdrantClient,
    embed_batch: int = EMBED_BATCH_SIZE,
    qdrant_batch: int = QDRANT_UPSERT_BATCH,
) -> int:
    """Stream listings from Postgres, embed, upsert to Qdrant.

    Reads from Postgres in cursor batches so memory use is O(batch).
    Returns total points upserted.
    """
    print("\n[embed-listings] starting…")
    t0 = time.time()
    total = 0

    async with pool.acquire() as conn:
        # Count for the progress bar.
        count = await conn.fetchval("SELECT COUNT(*) FROM listings")
        print(f"[embed-listings] {count} listings to embed.")

        # Stream via server-side cursor.
        async with conn.transaction():
            cur = conn.cursor(
                "SELECT id, name, type, city, neighbourhood, lat, lng, "
                "base_price, beds, amenities, photos, host, rating, review_count "
                "FROM listings",
                prefetch=embed_batch,
            )

            text_buf: list[str] = []
            row_buf: list[asyncpg.Record] = []

            async def flush_embed_batch():
                nonlocal total
                vectors = embed_texts(text_buf)
                points: list[PointStruct] = []
                for row, vec in zip(row_buf, vectors):
                    d = dict(row)
                    d["amenities"] = json.loads(d["amenities"]) if isinstance(d["amenities"], str) else d["amenities"]
                    points.append(_listing_to_point(d, vec))

                # Qdrant upsert in sub-batches.
                for qi in range(0, len(points), qdrant_batch):
                    await upsert_points_batch(
                        qdrant, COLLECTION_LISTINGS, points[qi : qi + qdrant_batch]
                    )
                total += len(points)
                text_buf.clear()
                row_buf.clear()

            with tqdm(total=count, unit="listing") as pbar:
                async for row in cur:
                    # Build the text representation for embedding.
                    amenities = row["amenities"]
                    if isinstance(amenities, str):
                        amenities = json.loads(amenities)
                    amenity_str = ", ".join(amenities) if amenities else ""
                    text = (
                        f"{row['name']}. {row['type']} in {row['city']}, "
                        f"{row['neighbourhood']}. "
                        f"Amenities: {amenity_str}. "
                        f"Price: {row['base_price']} per night. "
                        f"Beds: {row['beds']}."
                    )
                    text_buf.append(text)
                    row_buf.append(row)

                    if len(text_buf) >= embed_batch:
                        await flush_embed_batch()
                        pbar.update(embed_batch)

            if text_buf:
                await flush_embed_batch()
                pbar.update(len(text_buf))

    elapsed = time.time() - t0
    print(f"[embed-listings] upserted {total} points in {elapsed:.1f}s.")
    return total


# ---------------------------------------------------------------------------
# Stage: embed reviews → Qdrant
# ---------------------------------------------------------------------------

async def stage_embed_summaries(
    pool: asyncpg.Pool,
    qdrant: AsyncQdrantClient,
    embed_batch: int = EMBED_BATCH_SIZE,
    qdrant_batch: int = QDRANT_UPSERT_BATCH,
) -> int:
    """Embed per-property review summaries → Qdrant `summaries` (Option A).

    One point per listing (id derived from listing_id, payload {listing_id}).
    Replaces per-review embedding — the 200K reviews stay in Postgres (full-text).
    """
    import uuid as _uuid
    print("\n[embed-summaries] starting…")
    t0 = time.time()
    total = 0

    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM listing_summaries WHERE summary IS NOT NULL AND summary <> ''"
        )
        print(f"[embed-summaries] {count} summaries to embed.")

        async with conn.transaction():
            cur = conn.cursor(
                "SELECT listing_id, summary FROM listing_summaries "
                "WHERE summary IS NOT NULL AND summary <> ''",
                prefetch=embed_batch,
            )

            text_buf: list[str] = []
            id_buf: list[str] = []

            async def flush():
                nonlocal total
                vectors = embed_texts(text_buf)
                points: list[PointStruct] = []
                for lid, vec in zip(id_buf, vectors):
                    point_id = _uuid.UUID(lid).int >> 64
                    points.append(PointStruct(id=point_id, vector=vec, payload={"listing_id": lid}))
                for qi in range(0, len(points), qdrant_batch):
                    await upsert_points_batch(
                        qdrant, COLLECTION_SUMMARIES, points[qi : qi + qdrant_batch]
                    )
                total += len(points)
                text_buf.clear()
                id_buf.clear()

            with tqdm(total=count, unit="summary") as pbar:
                async for row in cur:
                    text_buf.append((row["summary"] or "")[:512])
                    id_buf.append(row["listing_id"])
                    if len(text_buf) >= embed_batch:
                        await flush()
                        pbar.update(embed_batch)
            if text_buf:
                remaining = len(text_buf)
                await flush()
                pbar.update(remaining)

    elapsed = time.time() - t0
    print(f"[embed-summaries] upserted {total} points in {elapsed:.1f}s.")
    return total


async def stage_embed_reviews(
    pool: asyncpg.Pool,
    qdrant: AsyncQdrantClient,
    embed_batch: int = EMBED_BATCH_SIZE,
    qdrant_batch: int = QDRANT_UPSERT_BATCH,
) -> int:
    """Stream reviews from Postgres, embed, upsert to Qdrant.

    Memory: O(embed_batch × avg_text_len + embed_batch × 384 floats).
    At batch=128: ~128 × 300 chars + 128 × 1.5 KB ≈ 230 KB — negligible.
    Returns total points upserted.
    """
    print("\n[embed-reviews] starting…")
    t0 = time.time()
    total = 0

    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM reviews")
        print(f"[embed-reviews] {count} reviews to embed.")

        async with conn.transaction():
            cur = conn.cursor(
                "SELECT id, listing_id, rating, language, date, sentiment, "
                "aspects, text FROM reviews",
                prefetch=embed_batch,
            )

            text_buf: list[str] = []
            row_buf: list[asyncpg.Record] = []

            async def flush():
                nonlocal total
                vectors = embed_texts(text_buf)
                points: list[PointStruct] = []
                for row, vec in zip(row_buf, vectors):
                    d = dict(row)
                    if isinstance(d.get("aspects"), str):
                        d["aspects"] = json.loads(d["aspects"])
                    # date → ISO string for Qdrant payload.
                    if d.get("date") and not isinstance(d["date"], str):
                        d["date"] = d["date"].isoformat()
                    points.append(_review_to_point(d, vec))

                for qi in range(0, len(points), qdrant_batch):
                    await upsert_points_batch(
                        qdrant, COLLECTION_REVIEWS, points[qi : qi + qdrant_batch]
                    )
                total += len(points)
                text_buf.clear()
                row_buf.clear()

            with tqdm(total=count, unit="review") as pbar:
                async for row in cur:
                    # Truncate for embedding only; full text stays in Postgres.
                    text_buf.append((row["text"] or "")[:REVIEW_EMBED_CHARS])
                    row_buf.append(row)
                    if len(text_buf) >= embed_batch:
                        await flush()
                        pbar.update(embed_batch)

            if text_buf:
                remaining = len(text_buf)
                await flush()
                pbar.update(remaining)

    elapsed = time.time() - t0
    print(f"[embed-reviews] upserted {total} points in {elapsed:.1f}s.")
    return total


# ---------------------------------------------------------------------------
# Stage: per-property summaries
# ---------------------------------------------------------------------------

async def stage_summaries(
    pool: asyncpg.Pool,
    use_llm: bool = False,
    llm_client=None,
    sample_reviews: int = 20,
) -> None:
    """Compute and store per-property review summaries.

    Streams listings; for each one fetches up to `sample_reviews` review texts
    and writes to listing_summaries (upsert).  No full-corpus materialisation.
    """
    print("\n[summaries] computing per-property summaries…")
    t0 = time.time()
    done = 0

    async with pool.acquire() as conn:
        listing_ids = await conn.fetch("SELECT id FROM listings")
        total = len(listing_ids)
        print(f"[summaries] {total} listings.")

        for (lid,) in tqdm(listing_ids, total=total, unit="listing"):
            rows = await conn.fetch(
                "SELECT text FROM reviews WHERE listing_id = $1 LIMIT $2",
                lid, sample_reviews,
            )
            texts = [r["text"] for r in rows]
            result = await summarize_property(
                lid, texts, use_llm=use_llm, llm_client=llm_client
            )
            await conn.execute(
                """
                INSERT INTO listing_summaries (listing_id, summary, aspect_avg, updated_at)
                VALUES ($1, $2, $3, now())
                ON CONFLICT (listing_id) DO UPDATE
                  SET summary    = EXCLUDED.summary,
                      aspect_avg = EXCLUDED.aspect_avg,
                      updated_at = EXCLUDED.updated_at
                """,
                lid,
                result["summary"],
                json.dumps(result["aspect_avg"]),
            )
            done += 1

    elapsed = time.time() - t0
    print(f"[summaries] done {done} summaries in {elapsed:.1f}s.")


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

async def verify_counts(
    pool: asyncpg.Pool,
    qdrant: AsyncQdrantClient,
) -> None:
    """Print count reconciliation across Postgres and Qdrant."""
    print("\n[verify] count reconciliation:")
    async with pool.acquire() as conn:
        n_listings_pg = await conn.fetchval("SELECT COUNT(*) FROM listings")
        n_reviews_pg  = await conn.fetchval("SELECT COUNT(*) FROM reviews")
        n_summaries   = await conn.fetchval("SELECT COUNT(*) FROM listing_summaries")
        n_enriched    = await conn.fetchval(
            "SELECT COUNT(*) FROM listings WHERE neighbourhood_price_pct IS NOT NULL"
        )

    listings_info  = await qdrant.get_collection(COLLECTION_LISTINGS)
    summaries_info = await qdrant.get_collection(COLLECTION_SUMMARIES)
    n_listings_q   = listings_info.points_count
    n_summaries_q  = summaries_info.points_count

    rows = [
        ("Postgres listings",               n_listings_pg),
        ("Postgres reviews",                n_reviews_pg),
        ("Postgres listing_summaries",      n_summaries),
        ("Listings with price percentile",  n_enriched),
        ("Qdrant listings collection",      n_listings_q),
        ("Qdrant summaries collection",     n_summaries_q),
    ]
    width = max(len(r[0]) for r in rows) + 2
    for label, count in rows:
        status = ""
        if label == "Postgres listings" and n_listings_q is not None:
            status = " OK" if count == n_listings_q else f" MISMATCH (qdrant={n_listings_q})"
        if label == "Postgres listing_summaries" and n_summaries_q is not None:
            status = " OK" if count == n_summaries_q else f" (qdrant summaries={n_summaries_q})"
        print(f"  {label:<{width}}: {count}{status}")


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

async def stage_snapshot(pool: asyncpg.Pool) -> None:
    """Export pg_dump + Qdrant snapshot for fast `docker compose up` restore.

    Delegates to scripts/export_data.sh — the single source of truth for the
    artifact format (Postgres custom-format dump via the postgres container's
    pg_dump + one Qdrant snapshot per collection over the HTTP API). pg_dump
    needs `docker compose exec`, which is a host operation, so when this runs
    from inside the ingestion container we print the host command instead of
    failing.
    """
    import shutil
    import subprocess

    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "export_data.sh"

    if shutil.which("docker") and script.exists():
        print(f"\n[snapshot] running {script} …")
        try:
            subprocess.run(["bash", str(script)], cwd=str(repo_root), check=True)
            return
        except subprocess.CalledProcessError as exc:
            print(f"[snapshot] export script failed ({exc}); see manual steps below.")

    print(
        "\n[snapshot] Run the export from the host (needs docker compose access):\n"
        "  bash scripts/export_data.sh\n"
        "This writes dumps/travel.dump + dumps/<collection>.snapshot, then publish\n"
        "with: bash scripts/publish_artifacts.sh\n"
    )


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

async def run(
    source: str,
    n_listings: int,
    n_reviews: int,
    seed: int,
    use_llm: bool,
    snapshot: bool,
    recreate_qdrant: bool,
    listing_quotas: dict[str, int] | None = None,
    review_quotas: dict[str, int] | None = None,
) -> None:
    """Run the full ingestion pipeline.

    Parameters
    ----------
    source          : 'real-csv' (default) or 'synthetic' (legacy).
    n_listings      : Total listing target (used for synthetic mode; for real-csv
                      mode, listing_quotas overrides this per city).
    n_reviews       : Total review target (used for synthetic mode; for real-csv
                      mode, review_quotas overrides this per city).
    listing_quotas  : Per-city listing counts for real-csv mode.
    review_quotas   : Per-city review counts for real-csv mode.
    """
    # City folders to process in real-csv mode.
    city_folders = list(_CITY_FOLDER_MAP.keys())  # ['amsterdam', 'lisbon', 'los angeles']

    total_listings = sum(listing_quotas.values()) if listing_quotas else n_listings
    total_reviews  = sum(review_quotas.values())  if review_quotas  else n_reviews

    print("=" * 60)
    print("Travel Discovery AI — Ingestion Pipeline")
    print(f"  Source    : {source}")
    print(f"  Scale     : {total_listings:,} listings / {total_reviews:,} reviews")
    if listing_quotas:
        for city, q in listing_quotas.items():
            rq = (review_quotas or {}).get(city, 0)
            print(f"    {city:<15}: {q:,} listings / {rq:,} reviews")
    print(f"  Seed      : {seed}")
    print(f"  LLM       : {'enabled' if use_llm else 'disabled (heuristic mode)'}")
    print(f"  DB        : {DATABASE_URL}")
    print(f"  Qdrant    : {QDRANT_URL}")
    print("=" * 60)

    t_start = time.time()

    # 1. Connect to stores.
    print("\n[init] connecting to Postgres…")
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=5)

    print("[init] connecting to Qdrant…")
    qdrant = AsyncQdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)

    try:
        # 2. Schema.
        async with pool.acquire() as conn:
            await apply_schema(conn)

        # 3. Wipe existing data (always when loading real CSVs to replace synthetic).
        if recreate_qdrant or source == "real-csv":
            print("\n[init] truncating existing listings/reviews (TRUNCATE CASCADE)…")
            async with pool.acquire() as conn:
                await conn.execute("TRUNCATE listings CASCADE")
            print("[init] tables cleared.")

        # 4. Qdrant collections (Option A: listings + summaries; NO reviews).
        await ensure_collection(qdrant, COLLECTION_LISTINGS,  recreate=recreate_qdrant)
        await ensure_collection(qdrant, COLLECTION_SUMMARIES, recreate=recreate_qdrant)
        # Payload indexes for the agents' hard-constraint filters — mandatory on
        # Qdrant Cloud (strict mode) and baked into the snapshot for restores.
        await ensure_payload_indexes(qdrant, COLLECTION_LISTINGS)
        # Drop any stale reviews collection from earlier runs — reviews now live
        # only in Postgres (full-text), so a leftover vector collection would feed
        # the review agent stale data.
        _existing = {c.name for c in (await qdrant.get_collections()).collections}
        if COLLECTION_REVIEWS in _existing:
            await qdrant.delete_collection(COLLECTION_REVIEWS)
            print(f"[qdrant] dropped unused '{COLLECTION_REVIEWS}' collection (Option A).")

        # 5. LLM client (optional).
        llm_client = None
        if use_llm:
            llm_client = await _make_llm_client()
            if llm_client is None:
                print("[llm] No API key found — running in heuristic mode.")

        # 6. Load listings + reviews.
        if source == "real-csv":
            city_id_maps = await stage_real_csv_listings(
                pool,
                city_folders=city_folders,
                quotas=listing_quotas or {},
                seed=seed,
            )
            await stage_real_csv_reviews(
                pool,
                city_folders=city_folders,
                city_id_maps=city_id_maps,
                review_quotas=review_quotas or {},
                seed=seed,
                use_llm=use_llm,
                llm_client=llm_client,
            )
        else:
            # Synthetic path (legacy).
            cfg = GenConfig(
                n_listings=n_listings,
                n_reviews=n_reviews,
                seed=seed,
                use_llm_reviews=False,
            )
            listing_ids = await stage_listings(pool, cfg)
            await stage_reviews(pool, cfg, listing_ids, use_llm=use_llm, llm_client=llm_client)

        # 7. Neighbourhood price percentile (pure SQL).
        print("\n[enrich] computing neighbourhood price percentiles…")
        async with pool.acquire() as conn:
            updated = await neighbourhood_price_percentile(conn)
        print(f"[enrich] price percentile updated for {updated} listings.")

        # 8. Per-property summaries (heuristic default; LLM optional).
        summaries_use_llm = use_llm and os.environ.get("LLM_SUMMARIES") == "1"
        await stage_summaries(pool, use_llm=summaries_use_llm, llm_client=llm_client)

        # 9. Embed listings → Qdrant.
        n_listing_points = await stage_embed_listings(pool, qdrant)

        # 10. Embed per-property summaries → Qdrant (Option A: reviews stay in
        #     Postgres full-text; we embed summaries, not individual reviews).
        n_summary_points = await stage_embed_summaries(pool, qdrant)

        # 11. Verification.
        await verify_counts(pool, qdrant)

        # 12. Optional snapshot.
        if snapshot:
            await stage_snapshot(pool)

    finally:
        await pool.close()
        await qdrant.close()

    elapsed = time.time() - t_start
    print(f"\n[done] Pipeline completed in {elapsed:.1f}s ({elapsed/60:.1f} min).")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Travel Discovery AI — ingestion pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  # DEV dry-run — real CSVs, ~2K listings / ~10K reviews\n"
            "  python ingest.py --recreate-qdrant\n\n"
            "  # FULL scale — real CSVs, all ~50K listings / ~200K reviews\n"
            "  python ingest.py --scale full --recreate-qdrant\n\n"
            "  # Custom scale (real-csv mode)\n"
            "  python ingest.py --n-listings 5000 --n-reviews 20000\n\n"
            "  # Synthetic data (legacy)\n"
            "  python ingest.py --source synthetic --n-listings 1000 --n-reviews 5000\n\n"
            "  # Enable LLM enrichments (requires GEMINI_API_KEY)\n"
            "  python ingest.py --use-llm\n\n"
            "  # Export snapshot\n"
            "  python ingest.py --snapshot\n"
        ),
    )

    parser.add_argument(
        "--source",
        choices=["real-csv", "synthetic"],
        default="real-csv",
        help="Data source: 'real-csv' (default) = Inside Airbnb CSVs; 'synthetic' = legacy generator.",
    )

    scale_group = parser.add_mutually_exclusive_group()
    scale_group.add_argument(
        "--scale",
        choices=["dev", "full"],
        default=None,
        help=(
            "Preset scale for real-csv mode. "
            "'dev' = ~2K listings / ~10K reviews (default); "
            "'full' = Amsterdam:10,480 + Lisbon:19,760 + LA:19,760 / ~200K reviews."
        ),
    )
    scale_group.add_argument(
        "--n-listings",
        type=int,
        default=None,
        help="Total listing count (split equally across 3 cities in real-csv mode).",
    )
    parser.add_argument(
        "--n-reviews",
        type=int,
        default=None,
        help="Total review count (split equally across 3 cities).",
    )
    parser.add_argument("--seed", type=int, default=42, help="RNG seed (default: 42).")
    parser.add_argument("--use-llm", action="store_true", help="Enable LLM enrichments.")
    parser.add_argument("--snapshot", action="store_true", help="Export pg_dump + Qdrant snapshot.")
    parser.add_argument(
        "--recreate-qdrant",
        action="store_true",
        help="Drop and recreate Qdrant collections (destructive).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    # Resolve per-city quotas.
    listing_quotas: dict[str, int] | None = None
    review_quotas:  dict[str, int] | None = None
    n_listings = 0
    n_reviews  = 0

    if args.source == "real-csv":
        if args.scale == "full":
            listing_quotas = dict(_CITY_FULL_QUOTAS)
        elif args.n_listings is not None:
            # Distribute evenly across 3 cities.
            per_city_l = args.n_listings // 3
            rem_l = args.n_listings % 3
            cities_ordered = list(_CITY_FOLDER_MAP.values())
            listing_quotas = {c: per_city_l + (1 if i < rem_l else 0)
                              for i, c in enumerate(cities_ordered)}
        else:
            # Default: dev scale.
            listing_quotas = dict(_CITY_DEV_QUOTAS)

        # Review quotas: default 5 reviews per listing per city.
        if args.n_reviews is not None:
            per_city_r = args.n_reviews // 3
            rem_r = args.n_reviews % 3
            cities_ordered = list(_CITY_FOLDER_MAP.values())
            review_quotas = {c: per_city_r + (1 if i < rem_r else 0)
                             for i, c in enumerate(cities_ordered)}
        elif args.scale == "full":
            review_quotas = {c: 66_667 for c in _CITY_FULL_QUOTAS}
        else:
            # Dev: ~5× listing quota per city.
            review_quotas = {c: q * 5 for c, q in listing_quotas.items()}

        n_listings = sum(listing_quotas.values())
        n_reviews  = sum(review_quotas.values())
    else:
        # Synthetic path.
        from generate import DEV_N_LISTINGS, DEV_N_REVIEWS, PROD_N_LISTINGS, PROD_N_REVIEWS
        if args.scale == "full":
            n_listings = PROD_N_LISTINGS
            n_reviews  = PROD_N_REVIEWS
        elif args.n_listings is not None:
            n_listings = args.n_listings
            n_reviews  = args.n_reviews or (args.n_listings * 5)
        else:
            n_listings = DEV_N_LISTINGS
            n_reviews  = args.n_reviews or DEV_N_REVIEWS

    asyncio.run(
        run(
            source=args.source,
            n_listings=n_listings,
            n_reviews=n_reviews,
            seed=args.seed,
            use_llm=args.use_llm,
            snapshot=args.snapshot,
            recreate_qdrant=args.recreate_qdrant,
            listing_quotas=listing_quotas,
            review_quotas=review_quotas,
        )
    )
