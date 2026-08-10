# Ingestion (Data Layer)

A re-runnable data ingestion pipeline. The primary source is real Inside Airbnb data for Amsterdam, Lisbon, and Los Angeles. Dev scale is about 2,000 listings / 10,000 reviews across the 3 cities; production scale is 50,000 listings / ~200,000 reviews.

Status: the real-CSV loader is complete, and the dev-scale dry-run is verified (see [Verified dry-run](#verified-dry-run) below).

## Layout

```
schema.sql       # Postgres schema: listings, reviews, listing_summaries (NO calendar table)
ingest.py        # Pipeline orchestrator: schema -> load CSVs -> enrich -> embed -> index
enrich.py        # Four ingest-time enrichments (see below)
availability.py  # Deterministic calendar function: availability + price, never stored
generate.py      # Synthetic data generator (legacy / testing only)
requirements.txt # Python dependencies including langdetect
```

## Source data

Inside Airbnb detailed CSVs at `csvData/<city>/listings.csv` and `reviews.csv`.
- `csvData/amsterdam/`: about 10,480 listings
- `csvData/lisbon/`: about 19,760 listings
- `csvData/los angeles/`: about 19,760 listings (note the space in the folder name)

calendar.csv is NOT loaded (130-620 MB, and Lisbon's has no price). Availability is computed deterministically by `availability.py`.

## Integration contract

- `listings.city`: `"Amsterdam"`, `"Lisbon"`, `"Los Angeles"` (verbatim).
- `listings.type`: the real `room_type` verbatim, so `"Entire home/apt"`, `"Private room"`, `"Shared room"`, `"Hotel room"`.
- 18 canonical amenity terms (unchanged): `wifi, pool, kitchen, parking, balcony, ac, gym, washer, pets_allowed, hot_tub, bbq, workspace, beach_access, concierge, breakfast_included, ev_charger, elevator, baby_cot`.
- Photos: the hero is the real `picture_url`, padded to 4-6 from a deterministic per-city pool of all the picture_urls. They're all real Airbnb CDN URLs (muscache.com).

## Sampling strategy

Dev scale (default): the top 660/670/670 listings per city ranked by `number_of_reviews` DESC, with seeded deterministic tie-breaking. Reviews are 5x the listing quota per city, round-robin interleaved across listings.

Full scale: Amsterdam is all 10,480; Lisbon is 19,760; Los Angeles is 19,760. Reviews are 66,667 per city.

## Field mapping

| Postgres `listings` | Source field |
|---|---|
| `id` | UUID v5 derived from the raw `id` |
| `name` | `name` |
| `type` | `room_type` verbatim |
| `city` | assigned from the folder name |
| `neighbourhood` | `neighbourhood_cleansed`, falling back to `neighbourhood` |
| `lat` / `lng` | `latitude` / `longitude` |
| `base_price` | `price` (strip $ and commas, impute the city+room_type median if missing) |
| `beds` | `beds`, then `bedrooms`, then `ceil(accommodates/2)`, min 1 |
| `amenities` | `json.loads(amenities)`, normalized to the 18-term vocab |
| `photos` | hero `picture_url` plus deterministic pool padding (at least 4) |
| `host` | `{id, name, superhost: host_is_superhost=='t'}` |
| `rating` | `review_scores_rating` (divide by 20 if >5, clamp 0-5, null is ok) |
| `review_count` | `number_of_reviews` |

| Postgres `reviews` | Source field |
|---|---|
| `id` | UUID v5 derived from the raw `id` |
| `listing_id` | mapped from the raw `listing_id` to its stable UUID |
| `date` | `date` |
| `reviewer` | `reviewer_name` |
| `rating` | null (reviews.csv has no per-review stars) |
| `text` | `comments` |
| `language` | `langdetect(comments[:500])` |
| `aspects` / `sentiment` | heuristic enrichment |

## Stores (the split), Option A vector scope

- Postgres (relational): all 50K listings, all 200K reviews (text, language, aspects, sentiment), and the 50K summaries. Reviews get a GIN full-text index (`idx_reviews_fts`, `'simple'` config) for keyword review search.
- Qdrant (vectors): `listings` (50K) and `summaries` (50K) at 384-dim, cosine, int8. Individual reviews are not embedded. Embedding 200K long real reviews is roughly 15 hours on a 4-core CPU, so I embed the per-property summaries instead and serve review search from Postgres full-text. (`stage_embed_summaries` builds the `summaries` collection, and a stale `reviews` collection gets dropped on `--recreate-qdrant`.) The root README's "Key trade-offs" has the latency/recall analysis.

## Enrichments

All four live in `enrich.py`:

1. Amenity normalization: maps the real Airbnb amenity strings (things like `"Free parking on premises"`, `"Dedicated workspace"`, `"Shared pool"`, `"Crib"`) onto the 18-term vocabulary. Pure Python, no LLM, idempotent, applied per-listing at insert time.
2. Aspect-level sentiment per review: scores `{cleanliness, location, value, staff, noise}` in `[-1, 1]` or null. The default is a keyword heuristic with a negation window (offline, zero cost). Optionally it can use Gemini Flash in batched mode (`--use-llm`). Applied per-review at insert time.
3. Neighbourhood price percentile: a single SQL `UPDATE … percent_rank() OVER (PARTITION BY city, neighbourhood ORDER BY base_price)`. Pure SQL, no LLM. Stored in `listings.neighbourhood_price_pct`.
4. Per-property review summary: `{summary: str, aspect_avg: {...}}`. The default is heuristic (a snippet plus mean scores). Stored in `listing_summaries`.

Language detection (the `langdetect` library) runs on every real review comment. It caps at 500 chars for speed and returns an ISO 639-1 code, or null on failure.

## Calendar is computed, not stored

`availability.py` returns a stable `{available, price}` for any `(listing_id, date)` via a deterministic hash, plus `is_available_range()` for `[check_in, check_out)`. That avoids materializing roughly 50K x 365, about 18M rows. The backend keeps a copy of this logic, so keep the two in sync (same hash, same params).

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
# DEV DRY-RUN: ~2,000 listings / ~10,000 reviews across 3 real cities (~10 min)
cd ingestion
python ingest.py --recreate-qdrant

# FULL SCALE: Amsterdam 10,480 + Lisbon 19,760 + LA 19,760 / ~200,001 reviews (~5 h on a 4-core CPU)
python ingest.py --scale full --recreate-qdrant

# Custom scale (evenly split across 3 cities)
python ingest.py --n-listings 5000 --n-reviews 20000

# Enable LLM enrichments (requires the GEMINI_API_KEY env var)
python ingest.py --use-llm

# Synthetic data (legacy/testing only)
python ingest.py --source synthetic --n-listings 1000 --n-reviews 5000

# Export pre-built artifacts (pg_dump + Qdrant snapshots) into dumps/
python ingest.py --snapshot          # delegates to scripts/export_data.sh
# ...or run the script directly from the host:
bash ../scripts/export_data.sh
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
QDRANT_COLLECTION_SUMMARIES=summaries          # Option A: summaries embedded, not reviews
EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
EMBEDDING_DIM=384
GEMINI_API_KEY=                   # only needed with --use-llm
```

## Verified dry-run (Option A, ~2K/10K)

```
Postgres listings               : 2000   OK
Postgres reviews                : 10000  (full-text in Postgres, not embedded)
Postgres listing_summaries      : 2000   OK
Listings with price percentile  : 2000
Qdrant listings collection      : 2000
Qdrant summaries collection     : 2000   (reviews collection: none)
idx_reviews_fts                 : present
Total pipeline time             : 7.5 min  (was 63 min before Option A + threads=4)
```

Quality spot-checks (verified):
- `type` is in ('Entire home/apt','Private room','Shared room','Hotel room'); `city` is in ('Amsterdam','Lisbon','Los Angeles'), with no Dubai.
- At least 4 real `muscache.com` photos per listing, and `base_price` > 0 (including imputed ones).
- Amenities normalized to the canonical terms; real multilingual review text with `language` set (en/fr/de/es/pt/nl and so on); `review_scores_rating` on a 0-5 scale.

## Scale and timing notes

- fastembed/ONNX (`bge-small-en-v1.5`, ~23 MB): no torch, no GPU needed, `threads=cpu_count` (about 1.6x over the default on a 4-core box).
- Embedding throughput on this 4-core CPU is roughly 5-10 short texts/sec, which is exactly why the reviews aren't embedded (see Stores).
- Dev scale (Option A, ~2K/10K, so ~4K vectors): about 7.5 min total.
- Full scale (Option A: 50K listings + 50K summaries = ~100K vectors): embedding ~3-4 h plus load/enrich ~2 h, so roughly 5 h CPU-only.
- The pipeline is safe to re-run: `TRUNCATE listings CASCADE` before each run (real-csv mode always wipes), and the upserts guard against partial re-inserts.
- It's deterministic: the same seed plus the same CSV content gives identical IDs and the same sampling selection.
- Run it once at full scale, then export a Postgres dump + Qdrant snapshot (`scripts/export_data.sh`) and publish them to a GitHub Release (`scripts/publish_artifacts.sh`), so `docker compose up` plus `scripts/restore_local.sh` restores in seconds without the raw CSVs.

## LLM cost (informational)

| Enrichment | Free (no --use-llm) | LLM mode (Gemini Flash free tier) | Gemini Flash paid est. |
|---|---|---|---|
| Aspect sentiment (200K reviews) | $0, heuristic | ~2.7 hrs (1,500 req/day limit) | ~$0.01 |
| Property summaries (50K listings) | $0, heuristic | ~33 days (free tier) | ~$1.75 |

LLM mode is optional. The heuristic mode produces scores that are good enough for the UI and the agents.

## Dubai removal

Dubai is fully retired:
- `generate.py`: the `CITIES` list is now `["Amsterdam", "Lisbon", "Los Angeles"]`, and `CITY_BOUNDS` carries the Amsterdam/Lisbon/Los Angeles bounds.
- `photo_pool.json`: kept as-is (it's a legacy file; the real CSV loader uses each listing's `picture_url` directly, so the primary path no longer consults `photo_pool.json`).
- There are no Dubai entries in the real CSV data (the `csvData/dubai/` folder was never present).
