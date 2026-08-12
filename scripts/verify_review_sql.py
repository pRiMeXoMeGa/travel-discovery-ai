#!/usr/bin/env python
"""Validate the review-snippet SQL against a real Postgres.

The queries in `backend/app/agents/review_intel.py` are built by pure functions
and unit-tested without a database, so their *shape* is covered but the SQL
itself has never been parsed by Postgres. This closes that gap: it runs EXPLAIN
on both builders with realistic parameters, which forces a full parse, column
and type resolution, and planning — without reading or writing any rows.

Usage (needs the local stack up: `docker compose up -d postgres`, data restored):

    python scripts/verify_review_sql.py

Or against any other instance:

    DATABASE_URL='postgresql://user:pass@host/db' python scripts/verify_review_sql.py

Exits non-zero on the first query Postgres rejects.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://travel:travel@localhost:5432/travel"
)

# Two real listing UUIDs are not required — EXPLAIN never dereferences them.
SAMPLE_IDS = [
    "e2ad228c-31fc-50b1-a679-674439dd64d4",
    "224e0713-53c2-57c5-be87-a6c074afe731",
]


async def main() -> int:
    try:
        import asyncpg
    except ImportError:
        print("!! asyncpg not installed — pip install asyncpg", file=sys.stderr)
        return 2

    from app.agents.review_intel import (
        build_focus_query,
        build_polarity_sample_query,
    )

    cases = [
        ("polarity sample (no focus)", build_polarity_sample_query(SAMPLE_IDS)),
        ("focus / full-text", build_focus_query(SAMPLE_IDS, "quiet at night", 8)),
        ("polarity sample (single listing)", build_polarity_sample_query(SAMPLE_IDS[:1])),
    ]

    try:
        conn = await asyncpg.connect(DATABASE_URL)
    except Exception as exc:  # noqa: BLE001
        print(f"!! could not connect: {exc}", file=sys.stderr)
        print("   is the stack up?  docker compose up -d postgres", file=sys.stderr)
        return 2

    failures = 0
    try:
        for label, (sql, params) in cases:
            try:
                await conn.fetch(f"EXPLAIN {sql}", *params)
                print(f"  OK    {label}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"  FAIL  {label}\n        {type(exc).__name__}: {exc}")
    finally:
        await conn.close()

    if failures:
        print(f"\n{failures} query/queries rejected by Postgres.")
        return 1

    print(f"\nAll {len(cases)} queries parse and plan cleanly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
