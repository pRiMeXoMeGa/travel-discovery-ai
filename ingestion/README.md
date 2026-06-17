# Ingestion — Data Layer

Re-runnable data generation + ingestion pipeline. Produces and loads the corpus: **≥50K listings + ≥200K reviews across ≥2 cities** (Lisbon, Dubai by default).

> **Status:** scaffold. Schema and the deterministic calendar are concrete; generation/enrichment/embedding bodies are `TODO`.

## Layout

```
schema.sql       # Postgres schema: listings, reviews, listing_summaries (NO calendar table)
generate.py      # synthetic data generation (Faker + optional LLM review text)
enrich.py        # ingest-time enrichments
availability.py  # deterministic calendar function (availability + price)
ingest.py        # pipeline orchestrator: schema → generate/load → enrich → embed → index
```

## Stores (the split)

- **Postgres (relational):** all 50K listings + all 200K reviews (text, rating, language, aspects, sentiment) + precomputed summaries.
- **Qdrant (vectors):** all listings + all reviews embedded = ~250K points @ **384-dim**, Cosine, int8 quantization.

This relational/vector split is the project's answer to the brief's "justify the store split."

## Enrichments (≥2 required; these are wired in `enrich.py`)

1. **Aspect-level sentiment** per review (cleanliness, location, value, staff, noise) → powers review topic filtering + aspect scores.
2. **Per-property review summary** → powers the "AI summary at top" + compare verdict.
3. **Neighbourhood price percentile** ("is this expensive for the area") → single SQL pass, no LLM.
4. **Amenity normalization** → consistent amenity filters.

Embeddings are produced in `ingest.py` (the Retrieval agent needs them).

## Calendar = computed, not stored

`availability.py` returns a stable `{available, price}` for any `(listing_id, date)` via a hash, plus `is_available_range()` for `[check_in, check_out)`. This avoids materializing ~50K × 365 ≈ **18M rows** (over the free-tier cap) while still behaving like a real calendar for the date filter. **The backend keeps a copy of this logic — keep the two in sync** (same hash, same params).

## Run

```bash
# via the root stack (Postgres + Qdrant must be up):
docker compose run --rm ingestion python ingest.py

# standalone:
pip install -r requirements.txt
python ingest.py            # needs DATABASE_URL + QDRANT_URL (see ../.env.example)
```

## Notes / scale

- **fastembed/ONNX** (same 384-dim `bge-small-en-v1.5` as the backend) — no torch.
- Embedding ~250K texts is the slow step (~30–90 min CPU; faster on GPU). Run once, then export a **Postgres dump + Qdrant snapshot** so `docker compose up` restores fast instead of re-ingesting.
- Stream/batch throughout — never load the full corpus into memory.
- Document any LLM cost incurred for synthetic review text / summaries (brief §2.1).
