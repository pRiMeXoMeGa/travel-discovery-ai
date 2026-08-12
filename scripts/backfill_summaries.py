#!/usr/bin/env python
"""WS0-A — replace heuristic property summaries with real LLM ones, for a subset.

The shipped `listing_summaries.summary` is not AI-generated. It is
`"Guests highlight: "` plus the first ~120 characters of two reviews, joined with
a semicolon (`ingestion/enrich.py::_heuristic_summary`) — and it is rendered in
the UI under a sparkle icon labelled "AI Review Summary", frequently truncated
mid-word. That is `FINDINGS.md` §2.1, and it is the most visible claim/reality
gap in the repo.

This backfills a SUBSET with genuine LLM summaries and records which is which.

    docker compose exec -T backend python - < scripts/backfill_summaries.py
    ... --limit 200 --dry-run

Three decisions worth knowing:

1. **Subset, not all 50K.** At ~700 tokens per listing the full corpus is ~35M
   tokens; on the free tier that is weeks of quota. The subset is chosen by
   ACTUAL review count, so the listings a demo is most likely to open — and the
   ones where a summary has enough evidence to be worth anything — are covered.

2. **Ordered by `count(*)` on `reviews`, NOT `listings.review_count`.** Those
   disagree: `review_count` is the Inside Airbnb reported figure carried through
   verbatim, and a listing reporting 1909 can have 3 rows (`FINDINGS.md` §6.7).
   Ordering by the reported column would pick listings with nothing to summarise.

3. **`aspect_avg` is never touched.** `agents/review_intel.py` feeds it to the
   answer model as "AGGREGATE ASPECT SCORES"; replacing per-review heuristic
   numbers with a model's guess would turn a grounded figure into a fabricated
   one. Only `summary` and `provenance` are written.

A `provenance` column is added if absent, so 'which summaries are real' is a
query rather than a guess.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/backend")

PROMPT = (
    "Summarise these guest reviews of one property in 2-3 sentences. "
    "Lead with what guests consistently praise, then what they complain about. "
    "Be factual and neutral, name specifics (location, cleanliness, noise, host), "
    "and do NOT invent anything not present in the reviews. "
    "If the reviews disagree, say so.\n\nReviews:\n{reviews}"
)
EMBED_BATCH = 64  # flush vectors this often, so a kill costs at most this many

SYSTEM = (
    "You write short, factual summaries of accommodation reviews. "
    "Never state anything the reviews do not support. No marketing language."
)


async def ensure_column(pool) -> None:
    async with pool.acquire() as con:
        await con.execute(
            "ALTER TABLE listing_summaries "
            "ADD COLUMN IF NOT EXISTS provenance text NOT NULL DEFAULT 'heuristic'"
        )
        # Which rows have had their VECTOR refreshed, as opposed to their text.
        # These are two separate stores and the job can die between them: an
        # interrupted run leaves fresh summaries in Postgres and stale heuristic
        # vectors in Qdrant, still matching queries, with nothing to say so. This
        # column makes that state a query (`summary_embedded_at < updated_at`)
        # instead of an assumption, and lets --reembed-only repair it.
        await con.execute(
            "ALTER TABLE listing_summaries "
            "ADD COLUMN IF NOT EXISTS summary_embedded_at timestamptz"
        )


async def stale_vector_ids(pool) -> list[str]:
    """LLM rows whose Qdrant vector is older than their text (or was never written)."""
    async with pool.acquire() as con:
        rows = await con.fetch(
            """
            SELECT listing_id::text AS id FROM listing_summaries
            WHERE provenance = 'llm'
              AND (summary_embedded_at IS NULL OR summary_embedded_at < updated_at)
            ORDER BY listing_id
            """
        )
    return [r["id"] for r in rows]


async def pick_listings(pool, limit: int, min_reviews: int) -> list[tuple[str, int]]:
    async with pool.acquire() as con:
        rows = await con.fetch(
            """
            SELECT r.listing_id::text AS id, count(*) AS n
            FROM reviews r
            JOIN listing_summaries s ON s.listing_id = r.listing_id
            WHERE s.provenance <> 'llm'
            GROUP BY r.listing_id
            HAVING count(*) >= $2
            ORDER BY count(*) DESC, r.listing_id
            LIMIT $1
            """,
            limit, min_reviews,
        )
    return [(r["id"], r["n"]) for r in rows]


async def fetch_reviews(pool, listing_id: str, cap: int = 20) -> list[str]:
    async with pool.acquire() as con:
        rows = await con.fetch(
            """
            SELECT text FROM reviews
            WHERE listing_id::text = $1 AND text IS NOT NULL AND length(text) > 40
            ORDER BY length(text) DESC
            LIMIT $2
            """,
            listing_id, cap,
        )
    return [r["text"] for r in rows]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--min-reviews", type=int, default=4,
                    help="skip listings with too little evidence to summarise")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-embed", action="store_true",
                    help="skip re-embedding into Qdrant (text only)")
    ap.add_argument("--reembed-only", action="store_true",
                    help="write no summaries; just repair vectors left stale by "
                         "an interrupted run")
    args = ap.parse_args()

    from app import llm
    from app.db import get_pool

    pool = await get_pool()
    await ensure_column(pool)

    if args.reembed_only:
        stale = await stale_vector_ids(pool)
        if not stale:
            print("no stale vectors — Qdrant matches Postgres")
            return 0
        print(f"{len(stale)} listing(s) with a summary newer than its vector")
        if args.dry_run:
            print("  … dry run, nothing written")
            return 0
        print(f"re-embedded {await reembed(stale)} summary vector(s)")
        return 0

    targets = await pick_listings(pool, args.limit, args.min_reviews)
    if not targets:
        print("nothing to do — every candidate already has an LLM summary")
        return 0
    print(f"{len(targets)} listing(s) to summarise "
          f"(>= {args.min_reviews} real reviews, most-reviewed first)")
    if args.dry_run:
        for lid, n in targets[:10]:
            print(f"  {lid}  {n} reviews")
        print("  … dry run, nothing written")
        return 0

    done = failed = embedded = 0
    t0 = time.perf_counter()
    updated_ids: list[str] = []
    pending: list[str] = []

    for lid, n in targets:
        reviews = await fetch_reviews(pool, lid)
        if not reviews:
            continue
        joined = "\n---\n".join(f"{i + 1}. {r[:400]}" for i, r in enumerate(reviews))
        try:
            summary = await llm.complete_text(PROMPT.format(reviews=joined), SYSTEM)
        except Exception as exc:  # noqa: BLE001
            # One listing failing must not abandon the batch — this is a long
            # offline job and free-tier 429s are expected.
            failed += 1
            print(f"  FAIL {lid}: {type(exc).__name__}: {exc}"[:110])
            continue

        summary = " ".join((summary or "").split())
        if not summary:
            failed += 1
            continue

        async with pool.acquire() as con:
            await con.execute(
                "UPDATE listing_summaries "
                "SET summary = $2, provenance = 'llm', updated_at = now() "
                "WHERE listing_id::text = $1",
                lid, summary,
            )
        updated_ids.append(lid)
        pending.append(lid)
        done += 1

        # Flush vectors as we go rather than once at the end. This is a long job
        # on a free tier; the first version embedded only after the final
        # listing, so being killed at 80% left ~1,000 rows with new text and
        # stale vectors. Now an interruption costs at most one batch, and
        # --reembed-only cleans up even that.
        if len(pending) >= EMBED_BATCH and not args.no_embed:
            embedded += await reembed(pending)
            pending.clear()
        if done % 50 == 0:
            print(f"  {done}/{len(targets)}  embedded={embedded}  "
                  f"({time.perf_counter() - t0:.0f}s)", flush=True)

    if pending and not args.no_embed:
        embedded += await reembed(pending)
        pending.clear()

    print(f"\nwrote {done} LLM summaries, {failed} failed, "
          f"{time.perf_counter() - t0:.0f}s")

    if args.no_embed and updated_ids:
        print("NOT re-embedded (--no-embed): Qdrant still holds the OLD summary "
              "vectors, so retrieval will not reflect the new text until it is. "
              "Run with --reembed-only to fix.")
    else:
        print(f"re-embedded {embedded} summary vector(s) into Qdrant")

    async with pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT count(*) FILTER (WHERE provenance = 'llm') AS llm, count(*) AS total "
            "FROM listing_summaries"
        )
    print(f"corpus now: {row['llm']}/{row['total']} summaries are LLM-generated")
    return 0


async def reembed(listing_ids: list[str]) -> int:
    """Replace the Qdrant summary vectors for the rewritten listings.

    Without this the new text exists only in Postgres: retrieval's WS0-H fusion
    searches the `summaries` collection, so it would keep matching against the
    old heuristic quotes and the rewrite would have no effect on results.
    """
    from app.config import settings
    from app.db import get_pool
    from app.embeddings import embed_texts
    from app.vectorstore import get_qdrant
    from qdrant_client import models as qmodels

    pool = await get_pool()
    async with pool.acquire() as con:
        rows = await con.fetch(
            "SELECT listing_id::text AS id, summary FROM listing_summaries "
            "WHERE listing_id::text = ANY($1::text[])",
            listing_ids,
        )
    if not rows:
        return 0

    client = get_qdrant()
    written = 0
    # 64 points per upsert read-timed-out against a 50K collection that is also
    # serving the API. Smaller batches finish inside the client's timeout.
    BATCH = 16
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        vectors = await embed_texts([r["summary"] for r in chunk])
        # Point ids must match what stage_embed_summaries used, or this inserts
        # duplicates alongside the originals instead of replacing them.
        points = [
            qmodels.PointStruct(
                id=_point_id(r["id"]),
                vector=vec,
                payload={"listing_id": r["id"]},
            )
            for r, vec in zip(chunk, vectors)
        ]
        for attempt in range(4):
            try:
                await client.upsert(
                    collection_name=settings.qdrant_collection_summaries,
                    points=points,
                )
                break
            except Exception as exc:  # noqa: BLE001
                # A read timeout does not mean the write was rejected — the
                # upsert is idempotent on point id, so retrying is safe.
                if attempt == 3:
                    print(f"  upsert gave up after 4 tries: {type(exc).__name__}")
                    return written
                await asyncio.sleep(2 * (attempt + 1))

        # Stamp THIS batch before starting the next one. Stamping once at the end
        # loses the record of every batch that already succeeded when a later one
        # fails — which is the same all-or-nothing bug this column exists to fix.
        async with pool.acquire() as con:
            await con.execute(
                "UPDATE listing_summaries SET summary_embedded_at = now() "
                "WHERE listing_id::text = ANY($1::text[])",
                [r["id"] for r in chunk],
            )
        written += len(points)
    return written


def _point_id(listing_id: str) -> int:
    """Mirror ingestion's point-id scheme so upserts REPLACE rather than add.

    `ingestion/ingest.py::stage_embed_summaries` uses `UUID(lid).int >> 64` — an
    INTEGER id, not the listing_id string. Getting this wrong does not error: it
    inserts a second point per listing, doubling the collection and leaving the
    stale heuristic vector in place to keep matching queries. Verified against
    live Qdrant (id=1071058921934976 for listing 0003ce1f-…).
    """
    import uuid as _uuid

    return _uuid.UUID(listing_id).int >> 64


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
