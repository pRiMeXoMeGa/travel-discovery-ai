"""Re-runnable ingestion pipeline (brief §2.1).

End-to-end: schema -> generate/load -> enrich -> embed -> index. Designed to
complete on a laptop and be streamed/batched (never load everything into memory).

Stores:
  - relational rows  -> Postgres (all 50K listings + all 200K reviews)
  - 384-dim vectors  -> Qdrant   (all listings + all reviews = ~250K points)

Usage:
    python ingest.py                # full pipeline
    python ingest.py --snapshot     # also export pg dump + qdrant snapshot
"""
import asyncio

# from fastembed import TextEmbedding
# import asyncpg
# from qdrant_client import AsyncQdrantClient


async def run() -> None:
    # 1) Schema -------------------------------------------------------------
    #    Apply ingestion/schema.sql to Postgres. Create Qdrant collections
    #    (listings, reviews) with size=384, distance=Cosine, int8 quantization.

    # 2) Generate / load ----------------------------------------------------
    #    generate.py (synthetic) or load real CSVs. Stream in batches.

    # 3) Insert relational rows (batched COPY/executemany) ------------------

    # 4) Enrich (enrich.py) -------------------------------------------------
    #    - amenity normalization (per listing)
    #    - aspect sentiment (per review, batched LLM)
    #    - per-property summaries (all or top-N + on-demand)
    #    - neighbourhood price percentile (single SQL pass)

    # 5) Embed + index (fastembed, batched) ---------------------------------
    #    - embed listing docs -> upsert to Qdrant `listings`
    #    - embed every review  -> upsert to Qdrant `reviews`
    #    Keep batches small; ~250K texts is the slow step (~30-90 min CPU).

    # 6) (optional) Snapshot ------------------------------------------------
    #    pg_dump + Qdrant snapshot so `docker compose up` restores fast.

    raise NotImplementedError("TODO: implement ingestion pipeline")


if __name__ == "__main__":
    asyncio.run(run())
