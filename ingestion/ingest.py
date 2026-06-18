"""Re-runnable ingestion pipeline (brief §2.1).

End-to-end: schema → generate → enrich → embed → index.
Never loads the full corpus into memory: all stages stream/batch.

Stores
------
- Postgres   : all listings + all reviews (relational)
- Qdrant     : all listings + all reviews embedded @ 384-dim (cosine, int8)

Usage
-----
    # Dev scale (default: 1 000 listings / 5 000 reviews)
    python ingest.py

    # Custom scale
    python ingest.py --n-listings 50000 --n-reviews 200000

    # Enable LLM enrichments (requires GEMINI_API_KEY)
    python ingest.py --use-llm

    # Export pg_dump + qdrant snapshot after ingestion
    python ingest.py --snapshot

Memory profile
--------------
Dev scale  (1 K listings /  5 K reviews): < 200 MB RAM.
Prod scale (50 K listings / 200 K reviews): < 1 GB RAM (streamed in 256-item batches).

Timing estimate (CPU only, no GPU)
-----------------------------------
Dev scale : < 5 min (including model download on first run).
Prod scale: 30–90 min for embedding 250 K texts on a modern laptop CPU.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import AsyncGenerator, Iterator

import asyncpg
from dotenv import load_dotenv
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
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

COLLECTION_LISTINGS = os.environ.get("QDRANT_COLLECTION_LISTINGS", "listings")
COLLECTION_REVIEWS  = os.environ.get("QDRANT_COLLECTION_REVIEWS",  "reviews")

EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
VECTOR_DIM = 384
DISTANCE = Distance.COSINE

# Batch sizes — tuned for the memory/throughput sweet-spot on a laptop.
# Increasing DB_BATCH reduces round-trips; keep ≤ 500 to avoid asyncpg param limits.
DB_BATCH_LISTINGS  = 256
DB_BATCH_REVIEWS   = 256
EMBED_BATCH_SIZE   = 128   # fastembed processes this many texts at a time in ONNX
QDRANT_UPSERT_BATCH = 256  # Qdrant HTTP payload size; 256 × 384 floats ≈ 400 KB

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


# ---------------------------------------------------------------------------
# fastembed — lazy singleton so the model loads once
# ---------------------------------------------------------------------------

_embed_model = None


def get_embed_model():
    global _embed_model
    if _embed_model is None:
        from fastembed import TextEmbedding
        print(f"[embed] loading model '{EMBEDDING_MODEL}' (first run downloads ~23 MB)…")
        _embed_model = TextEmbedding(model_name=EMBEDDING_MODEL)
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
    if isinstance(d, str):
        d = _date.fromisoformat(d)
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
# Stage: generate + insert listings
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
                    text_buf.append(row["text"])
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

    listings_info = await qdrant.get_collection(COLLECTION_LISTINGS)
    reviews_info  = await qdrant.get_collection(COLLECTION_REVIEWS)
    n_listings_q  = listings_info.points_count
    n_reviews_q   = reviews_info.points_count

    rows = [
        ("Postgres listings",               n_listings_pg),
        ("Postgres reviews",                n_reviews_pg),
        ("Postgres listing_summaries",      n_summaries),
        ("Listings with price percentile",  n_enriched),
        ("Qdrant listings collection",      n_listings_q),
        ("Qdrant reviews collection",       n_reviews_q),
    ]
    width = max(len(r[0]) for r in rows) + 2
    for label, count in rows:
        status = ""
        if label == "Postgres listings" and n_listings_q is not None:
            status = " OK" if count == n_listings_q else f" MISMATCH (qdrant={n_listings_q})"
        if label == "Postgres reviews" and n_reviews_q is not None:
            status = " OK" if count == n_reviews_q else f" MISMATCH (qdrant={n_reviews_q})"
        print(f"  {label:<{width}}: {count}{status}")


# ---------------------------------------------------------------------------
# Snapshot (placeholder)
# ---------------------------------------------------------------------------

async def stage_snapshot(pool: asyncpg.Pool) -> None:
    """Export pg_dump + Qdrant snapshot for fast `docker compose up` restore.

    Not implemented in Phase 1 — wired as a placeholder so --snapshot is
    accepted without error.  The actual implementation depends on having
    docker-in-docker or exec access to the Postgres container.
    """
    print(
        "\n[snapshot] --snapshot requested. Placeholder: pg_dump and Qdrant snapshot "
        "export will be implemented in Phase 2.  For now, manually run:\n"
        "  docker compose exec postgres pg_dump -U travel travel > backup.sql\n"
        "  curl -X POST http://localhost:6333/collections/listings/snapshots\n"
        "  curl -X POST http://localhost:6333/collections/reviews/snapshots\n"
    )


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

async def run(
    n_listings: int,
    n_reviews: int,
    seed: int,
    use_llm: bool,
    snapshot: bool,
    recreate_qdrant: bool,
) -> None:
    cfg = GenConfig(
        n_listings=n_listings,
        n_reviews=n_reviews,
        seed=seed,
        # Review TEXT always comes from the offline multilingual templates.
        # `--use-llm` controls ENRICHMENT (aspect sentiment) only — it must NOT
        # turn on use_llm_reviews, which emits unimplemented [LLM_PLACEHOLDER]
        # text. (LLM-generated review text is a separate, unbuilt feature.)
        use_llm_reviews=False,
    )

    print("=" * 60)
    print(f"Travel Discovery AI — Ingestion Pipeline")
    print(f"  Scale     : {n_listings:,} listings / {n_reviews:,} reviews")
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

        # 3. Qdrant collections.
        await ensure_collection(qdrant, COLLECTION_LISTINGS, recreate=recreate_qdrant)
        await ensure_collection(qdrant, COLLECTION_REVIEWS,  recreate=recreate_qdrant)

        # 4. LLM client (optional).
        llm_client = None
        if use_llm:
            llm_client = await _make_llm_client()
            if llm_client is None:
                print("[llm] No API key found — running in heuristic mode.")

        # 5. Generate + insert listings.
        listing_ids = await stage_listings(pool, cfg)

        # 6. Generate + insert reviews (with aspect sentiment).
        await stage_reviews(pool, cfg, listing_ids, use_llm=use_llm, llm_client=llm_client)

        # 7. Neighbourhood price percentile (pure SQL).
        print("\n[enrich] computing neighbourhood price percentiles…")
        async with pool.acquire() as conn:
            updated = await neighbourhood_price_percentile(conn)
        print(f"[enrich] price percentile updated for {updated} listings.")

        # 8. Per-property summaries.
        # Kept heuristic even when --use-llm is set: summaries are ~750 calls
        # (the bulk of LLM usage) and lower-value than aspect sentiment, so we
        # conserve the free-tier daily quota for aspects + the runtime concierge.
        # Flip to use_llm=use_llm (or gate on LLM_SUMMARIES) to LLM-summarize.
        summaries_use_llm = use_llm and os.environ.get("LLM_SUMMARIES") == "1"
        await stage_summaries(pool, use_llm=summaries_use_llm, llm_client=llm_client)

        # 9. Embed listings → Qdrant.
        n_listing_points = await stage_embed_listings(pool, qdrant)

        # 10. Embed reviews → Qdrant.
        n_review_points = await stage_embed_reviews(pool, qdrant)

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
    from generate import DEV_N_LISTINGS, DEV_N_REVIEWS, PROD_N_LISTINGS, PROD_N_REVIEWS

    parser = argparse.ArgumentParser(
        description="Travel Discovery AI — ingestion pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python ingest.py                          # dev scale (1 K / 5 K)\n"
            "  python ingest.py --scale full             # 50 K / 200 K\n"
            "  python ingest.py --n-listings 5000 --n-reviews 20000\n"
            "  python ingest.py --use-llm --snapshot\n"
        ),
    )
    scale_group = parser.add_mutually_exclusive_group()
    scale_group.add_argument(
        "--scale",
        choices=["dev", "full"],
        default=None,
        help="'dev' = 1K/5K (default); 'full' = 50K/200K.",
    )
    scale_group.add_argument("--n-listings", type=int, default=None)
    parser.add_argument("--n-reviews", type=int, default=None)
    parser.add_argument("--seed", type=int, default=42, help="RNG seed (default: 42).")
    parser.add_argument("--use-llm", action="store_true", help="Enable LLM enrichments.")
    parser.add_argument("--snapshot", action="store_true", help="Export pg_dump + Qdrant snapshot.")
    parser.add_argument("--recreate-qdrant", action="store_true",
                        help="Drop and recreate Qdrant collections (destructive).")
    return parser.parse_args()


if __name__ == "__main__":
    from generate import DEV_N_LISTINGS, DEV_N_REVIEWS, PROD_N_LISTINGS, PROD_N_REVIEWS

    args = _parse_args()

    if args.scale == "full":
        n_listings = PROD_N_LISTINGS
        n_reviews  = PROD_N_REVIEWS
    elif args.n_listings is not None:
        n_listings = args.n_listings
        n_reviews  = args.n_reviews or (args.n_listings * 5)
    else:
        # Default: dev scale.
        n_listings = DEV_N_LISTINGS
        n_reviews  = args.n_reviews or DEV_N_REVIEWS

    asyncio.run(
        run(
            n_listings=n_listings,
            n_reviews=n_reviews,
            seed=args.seed,
            use_llm=args.use_llm,
            snapshot=args.snapshot,
            recreate_qdrant=args.recreate_qdrant,
        )
    )
