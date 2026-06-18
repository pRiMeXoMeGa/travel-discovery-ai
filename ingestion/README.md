# Ingestion — Data Layer

Re-runnable data generation + ingestion pipeline. Produces and loads the corpus: **1 000 listings / 5 000 reviews** at dev scale (default), scaling to **50 000 listings / 200 000 reviews** in production.

> **Status:** Phase 1 complete & verified at dev scale — schema, synthetic data with **real city-matched photos**, four enrichments (aspect sentiment in **heuristic mode**, see trade-off below), Postgres inserts, and Qdrant embeddings. Counts reconcile across stores.

## Layout

```
schema.sql       # Postgres schema: listings, reviews, listing_summaries (NO calendar table)
generate.py      # Synthetic data generation — Faker + per-city bounding boxes, Zipf review distribution
enrich.py        # Four ingest-time enrichments (see below)
availability.py  # Deterministic calendar function — availability + price, never stored
ingest.py        # Pipeline orchestrator: schema → generate → enrich → embed → index
photo_pool.json  # Curated real Airbnb-CDN photo URLs (Lisbon/Dubai), assigned per-listing
```

## Photos

Listings use **real Airbnb-CDN image URLs** (not placeholders). `generate.py` loads `photo_pool.json` (≈24.6K Lisbon + 10.4K Dubai URLs, derived from a raw CSV) and assigns 5–8 **city-matched** photos per listing deterministically by hashing the listing id. A shared pool is normal for stock-style booking imagery; the raw CSV is not needed at runtime (the pool file is). Large originals are downscaled at the frontend (`next/image`).

## Stores (the split)

- **Postgres (relational):** all listings + all reviews (text, rating, language, aspects, sentiment) + precomputed summaries.
- **Qdrant (vectors):** all listings + all reviews embedded = ~250K points @ **384-dim**, Cosine, int8 quantization.

This relational/vector split is the project's answer to the brief's "justify the store split."

## Enrichments

All four are wired in `enrich.py`:

1. **Amenity normalization** — maps free-form strings to an 18-term canonical vocabulary (e.g., "Jacuzzi" → `hot_tub`). Pure Python, no LLM, idempotent. Applied per-listing at insert time.
2. **Aspect-level sentiment per review** — scores `{cleanliness, location, value, staff, noise}` in `[-1, 1]` or `null`. Default: keyword heuristic with negation window (offline, zero cost). Optional: Gemini Flash batched mode (25 reviews/prompt). Applied per-review at insert time.
3. **Neighbourhood price percentile** — single SQL `UPDATE … percent_rank() OVER (PARTITION BY city, neighbourhood ORDER BY base_price)`. Pure SQL, no LLM, O(n). Stored in `listings.neighbourhood_price_pct`.
4. **Per-property review summary** — `{summary: str, aspect_avg: {...}}`. Default: heuristic (snippet + mean scores). Optional: Gemini Flash (~700 tokens/listing). Stored in `listing_summaries`.

**Trade-off — aspect sentiment + summaries run in heuristic mode (chosen, not just default).** The free-tier Gemini quota could not reliably enrich thousands of reviews (429s under volume), and the short templated review text limits the LLM's marginal value, so heuristic mode is what's loaded: ~30% of reviews carry aspects (English ~62%; non-English null). The LLM path stays fully built — throttled + retry-on-429 — behind `--use-llm` (and `LLM_SUMMARIES=1` for summaries) for the paid tier. Heuristic mode is also fully offline (no API key needed).

## Calendar = computed, not stored

`availability.py` returns a stable `{available, price}` for any `(listing_id, date)` via a deterministic hash, plus `is_available_range()` for `[check_in, check_out)`. This avoids materializing ~50K × 365 ≈ **18M rows**. **The backend keeps a copy of this logic — keep the two in sync** (same hash, same params).

## Run

### Prerequisites

```bash
# Install Python dependencies
pip install -r requirements.txt

# Start Postgres and Qdrant (from project root)
docker compose up -d postgres qdrant

# If you have a native Postgres on port 5432, use the override (already in repo):
# docker-compose.override.yml remaps the container to port 5433.
# Set DATABASE_URL accordingly (see Environment below).
```

### Auth setup (first time only per pgdata volume)

The postgres:16-alpine default is `scram-sha-256` but asyncpg 0.30 requires `md5` on Windows. Run once after a fresh container start:

```bash
docker exec travel-discovery-ai-postgres-1 sh -c \
  "sed -i 's/host all all all scram-sha-256/host all all all md5/' /var/lib/postgresql/data/pg_hba.conf && \
   echo 'password_encryption = md5' >> /var/lib/postgresql/data/postgresql.conf && \
   psql -U travel -c 'SELECT pg_reload_conf();' && \
   psql -U travel -c \"ALTER USER travel WITH PASSWORD 'travel';\""
```

### Running the pipeline

```bash
# Dev scale (default: 1K listings / 5K reviews, ~4 min)
python ingest.py

# Custom scale
python ingest.py --n-listings 5000 --n-reviews 20000

# Full production scale (50K / 200K, ~30-90 min CPU)
python ingest.py --scale full

# Enable LLM enrichments (requires GEMINI_API_KEY env var)
python ingest.py --use-llm

# Recreate Qdrant collections from scratch (destructive)
python ingest.py --recreate-qdrant

# Export snapshot (placeholder — Phase 2)
python ingest.py --snapshot
```

### Via Docker (production-style)

```bash
# From the project root, with stores already running:
docker compose run --rm ingestion python ingest.py
# Full scale:
docker compose run --rm ingestion python ingest.py --scale full
```

## Environment

```
DATABASE_URL=postgresql://travel:travel@localhost:5433/travel   # host with override
DATABASE_URL=postgresql://travel:travel@postgres:5432/travel    # inside Docker
QDRANT_URL=http://localhost:6333                                 # host
QDRANT_URL=http://qdrant:6333                                   # inside Docker
QDRANT_COLLECTION_LISTINGS=listings
QDRANT_COLLECTION_REVIEWS=reviews
EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
EMBEDDING_DIM=384
GEMINI_API_KEY=                   # only needed with --use-llm
```

## Verified dev-scale run (2026-06-18, heuristic mode + real photos)

```
Postgres listings               : 1,000   OK
Postgres reviews                : 5,000   OK   (0 placeholder text)
Postgres listing_summaries      : 1,000
Listings with price percentile  : 1,000
Listings with real photos       : 1,000   (Airbnb CDN, city-matched)
Reviews with aspect scores      : 1,484   (heuristic; English-mostly)
Qdrant listings collection      : 1,000   OK
Qdrant reviews collection       : 5,000   OK

Total pipeline time: 208s (3.5 min)
```

## Scale / timing notes

- **fastembed/ONNX** (`bge-small-en-v1.5`, ~23 MB) — no torch, no GPU required.
- Embedding throughput: ~30 texts/sec on a laptop CPU.
- At full scale (250K texts): estimate **90–140 min** CPU-only; faster on GPU via `fastembed` CUDA support.
- The pipeline is safe to re-run: upserts (`ON CONFLICT DO NOTHING` in Postgres, Qdrant `wait=True`), deterministic IDs from seed 42.
- Run once at full scale, then export a **Postgres dump + Qdrant snapshot** so `docker compose up` restores in seconds (Phase 2).

## LLM cost (informational)

| Enrichment | Free (no --use-llm) | LLM mode (Gemini Flash free tier) | Gemini Flash paid est. |
|---|---|---|---|
| Aspect sentiment (200K reviews) | $0 — heuristic | ~2.7 hrs (1 500 req/day limit) | ~$0.01 |
| Property summaries (50K listings) | $0 — heuristic | ~33 days (free tier) | ~$1.75 |

LLM mode is optional — heuristic mode produces usable scores for the UI and agents.
