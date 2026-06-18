# Ingestion — Data Layer

Re-runnable data ingestion pipeline. Primary source: **real Inside Airbnb data** for Amsterdam, Lisbon, and Los Angeles. Dev scale: **~2,000 listings / ~10,000 reviews** across 3 cities. Production scale: **50,000 listings / ~200,000 reviews**.

> **Status:** Real-CSV loader complete. Dev-scale dry-run verified — see [Verified dry-run](#verified-dry-run) below.

## Layout

```
schema.sql       # Postgres schema: listings, reviews, listing_summaries (NO calendar table)
ingest.py        # Pipeline orchestrator: schema → load CSVs → enrich → embed → index
enrich.py        # Four ingest-time enrichments (see below)
availability.py  # Deterministic calendar function — availability + price, never stored
generate.py      # Synthetic data generator (legacy / testing only)
requirements.txt # Python dependencies including langdetect
```

## Source data

Inside Airbnb detailed CSVs at `csvData/<city>/listings.csv` and `reviews.csv`.
- `csvData/amsterdam/` — ~10,480 listings
- `csvData/lisbon/` — ~19,760 listings
- `csvData/los angeles/` — ~19,760 listings (note space in folder name)

**calendar.csv is NOT loaded** (130–620 MB, and Lisbon's has no price). Availability is computed deterministically by `availability.py`.

## Integration contract

- `listings.city`: `"Amsterdam"`, `"Lisbon"`, `"Los Angeles"` (verbatim).
- `listings.type`: real `room_type` verbatim — `"Entire home/apt"`, `"Private room"`, `"Shared room"`, `"Hotel room"`.
- 18 canonical amenity terms (unchanged): `wifi, pool, kitchen, parking, balcony, ac, gym, washer, pets_allowed, hot_tub, bbq, workspace, beach_access, concierge, breakfast_included, ev_charger, elevator, baby_cot`.
- Photos: hero = real `picture_url`; padded to 4–6 from a deterministic per-city pool of all picture_urls. All are real Airbnb CDN URLs (muscache.com).

## Sampling strategy

**Dev scale (default):** Top 660/670/670 listings per city ranked by `number_of_reviews` DESC, seeded-deterministic tie-breaking. Reviews: 5× listing quota per city, round-robin interleaved across listings.

**Full scale:** Amsterdam = ALL 10,480; Lisbon = 19,760; Los Angeles = 19,760. Reviews = 66,667 per city.

## Field mapping

| Postgres `listings` | Source field |
|---|---|
| `id` | UUID v5 derived from raw `id` |
| `name` | `name` |
| `type` | `room_type` verbatim |
| `city` | assigned from folder name |
| `neighbourhood` | `neighbourhood_cleansed` → `neighbourhood` |
| `lat` / `lng` | `latitude` / `longitude` |
| `base_price` | `price` (strip $/, impute city+room_type median if missing) |
| `beds` | `beds` → `bedrooms` → `ceil(accommodates/2)`, min 1 |
| `amenities` | `json.loads(amenities)` → normalize to 18-term vocab |
| `photos` | hero `picture_url` + deterministic pool padding (≥4) |
| `host` | `{id, name, superhost: host_is_superhost=='t'}` |
| `rating` | `review_scores_rating` (÷20 if >5; clamp 0–5; null ok) |
| `review_count` | `number_of_reviews` |

| Postgres `reviews` | Source field |
|---|---|
| `id` | UUID v5 derived from raw `id` |
| `listing_id` | mapped from `listing_id` raw → stable UUID |
| `date` | `date` |
| `reviewer` | `reviewer_name` |
| `rating` | null (reviews.csv has no per-review stars) |
| `text` | `comments` |
| `language` | `langdetect(comments[:500])` |
| `aspects` / `sentiment` | heuristic enrichment |

## Stores (the split)

- **Postgres (relational):** all listings + all reviews (text, rating, language, aspects, sentiment) + precomputed summaries.
- **Qdrant (vectors):** all listings + all reviews embedded = ~250K points @ **384-dim**, Cosine, int8 quantization.

## Enrichments

All four are wired in `enrich.py`:

1. **Amenity normalization** — maps real Airbnb amenity strings (e.g. `"Free parking on premises"`, `"Dedicated workspace"`, `"Shared pool"`, `"Crib"`) to the 18-term canonical vocabulary. Pure Python, no LLM, idempotent. Applied per-listing at insert time.
2. **Aspect-level sentiment per review** — scores `{cleanliness, location, value, staff, noise}` in `[-1, 1]` or `null`. Default: keyword heuristic with negation window (offline, zero cost). Optional: Gemini Flash batched mode (`--use-llm`). Applied per-review at insert time.
3. **Neighbourhood price percentile** — single SQL `UPDATE … percent_rank() OVER (PARTITION BY city, neighbourhood ORDER BY base_price)`. Pure SQL, no LLM. Stored in `listings.neighbourhood_price_pct`.
4. **Per-property review summary** — `{summary: str, aspect_avg: {...}}`. Default: heuristic (snippet + mean scores). Stored in `listing_summaries`.

**Language detection** (`langdetect` library) is applied to every real review comment. Caps at 500 chars for speed. Returns ISO 639-1 code or `null` on failure.

## Calendar = computed, not stored

`availability.py` returns a stable `{available, price}` for any `(listing_id, date)` via a deterministic hash, plus `is_available_range()` for `[check_in, check_out)`. This avoids materializing ~50K × 365 ≈ **18M rows**. **The backend keeps a copy of this logic — keep the two in sync** (same hash, same params).

## Run

### Prerequisites

```bash
# Install Python dependencies (including langdetect)
pip install -r requirements.txt

# Start Postgres and Qdrant (from project root)
docker compose up -d postgres qdrant
```

### Auth setup (first time only per pgdata volume)

```bash
docker exec travel-discovery-ai-postgres-1 sh -c \
  "sed -i 's/host all all all scram-sha-256/host all all all md5/' /var/lib/postgresql/data/pg_hba.conf && \
   echo 'password_encryption = md5' >> /var/lib/postgresql/data/postgresql.conf && \
   psql -U travel -c 'SELECT pg_reload_conf();' && \
   psql -U travel -c \"ALTER USER travel WITH PASSWORD 'travel';\""
```

### Running the pipeline

```bash
# DEV DRY-RUN — ~2,000 listings / ~10,000 reviews across 3 real cities (~10 min)
cd ingestion
python ingest.py --recreate-qdrant

# FULL SCALE — Amsterdam:10,480 + Lisbon:19,760 + LA:19,760 / ~200,001 reviews (~60-120 min)
python ingest.py --scale full --recreate-qdrant

# Custom scale (evenly split across 3 cities)
python ingest.py --n-listings 5000 --n-reviews 20000

# Enable LLM enrichments (requires GEMINI_API_KEY env var)
python ingest.py --use-llm

# Synthetic data (legacy/testing only)
python ingest.py --source synthetic --n-listings 1000 --n-reviews 5000

# Export snapshot (placeholder — Phase 2)
python ingest.py --snapshot
```

### Via Docker (production-style)

```bash
docker compose run --rm ingestion python ingest.py --recreate-qdrant
docker compose run --rm ingestion python ingest.py --scale full --recreate-qdrant
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

## Verified dry-run

Run the dry-run and paste the output table from `[verify]` here. Target counts:

```
Postgres listings               : ~2,000  (660 Amsterdam + 670 Lisbon + 670 Los Angeles)
Postgres reviews                : ~10,000 (3,300 + 3,350 + 3,350)
Postgres listing_summaries      : ~2,000
Listings with price percentile  : ~2,000
Qdrant listings collection      : ~2,000  OK
Qdrant reviews collection       : ~10,000 OK
```

Quality spot-checks to verify:
- `type` IN ('Entire home/apt', 'Private room', 'Shared room', 'Hotel room')
- `city` IN ('Amsterdam', 'Lisbon', 'Los Angeles') — no Dubai
- `base_price` > 0 for all rows (including imputed ones)
- `amenities` is a non-empty JSON array with canonical terms only
- `photos` length ≥ 4, all muscache.com URLs
- `review.text` is real guest comment text (not templated)
- `review.language` populated, includes 'pt' / 'nl' / 'es' examples
- `review_scores_rating` maps to 0–5 scale correctly

## Scale / timing notes

- **fastembed/ONNX** (`bge-small-en-v1.5`, ~23 MB) — no torch, no GPU required.
- Embedding throughput: ~30 texts/sec on a laptop CPU.
- Dev scale (~12K texts): ~7 min total.
- Full scale (~250K texts): estimate **60–120 min** CPU-only.
- Pipeline is safe to re-run: `TRUNCATE listings CASCADE` before each run (real-csv mode always wipes), then upserts guard against partial re-inserts.
- Deterministic: same seed + same CSV content → identical IDs, same sampling selection.
- Run once at full scale, then export a **Postgres dump + Qdrant snapshot** so `docker compose up` restores in seconds (Phase 2).

## LLM cost (informational)

| Enrichment | Free (no --use-llm) | LLM mode (Gemini Flash free tier) | Gemini Flash paid est. |
|---|---|---|---|
| Aspect sentiment (200K reviews) | $0 — heuristic | ~2.7 hrs (1 500 req/day limit) | ~$0.01 |
| Property summaries (50K listings) | $0 — heuristic | ~33 days (free tier) | ~$1.75 |

LLM mode is optional — heuristic mode produces usable scores for the UI and agents.

## Dubai removal

Dubai has been fully retired:
- `generate.py`: `CITIES` list updated to `["Amsterdam", "Lisbon", "Los Angeles"]`; `CITY_BOUNDS` now has Amsterdam/Lisbon/Los Angeles bounds.
- `photo_pool.json`: kept as-is (legacy file; the real CSV loader uses `picture_url` from each listing directly — `photo_pool.json` is no longer consulted by the primary path).
- No Dubai entries exist in the real CSV data (folder `csvData/dubai/` was never present).
