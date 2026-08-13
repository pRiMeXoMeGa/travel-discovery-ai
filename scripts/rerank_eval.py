#!/usr/bin/env python
"""Measure what cross-encoder reranking would actually buy (WS4).

Runs the golden queries through retrieval twice — once with the bi-encoder
ordering the app ships, once reranked — and reports how much the ordering
changes and what it costs.

Why offline: the reranker costs +156 MB resident and 20.8s to load, against
~33 MB of headroom on the 512 MB instance, so it is OFF in the API by default
(see app/rerank.py). This script loads it in a throwaway process instead, so the
trade-off can be documented with numbers without putting production at risk.

    docker compose exec -T backend python - < scripts/rerank_eval.py

What it reports, and what it does not:

  * rank displacement — how far the top-10 moves. A near-zero delta would mean
    reranking is not worth 156 MB regardless of anything else.
  * overlap@10 — how much of the top-10 set is unchanged.
  * latency and RSS — the costs, measured in the same run.

It does NOT report relevance. Nothing here knows which ordering is *better* —
that needs human judgement against EVAL.md's rubric, and a script claiming
otherwise would be inventing a ground truth it does not have. The numbers below
size the effect; a person still has to score it.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/backend")

# Golden queries from EVAL.md that exercise retrieval ordering. The NL-search
# and adversarial ones are omitted: they do not produce a ranked candidate set
# whose order reranking could change.
QUERIES = [
    ("Q1", "an entire place in Lisbon under 130 with a balcony"),
    ("Q2", "an entire place in Amsterdam near the centre under 200 a night"),
    ("Q4", "family-friendly place in Amsterdam with a pool and kitchen under 250"),
    ("Q5", "places in Lisbon guests say are quiet and clean"),
    ("extra", "a quiet studio in Lisbon near the metro with fast wifi"),
    ("extra", "somewhere in Los Angeles near the beach with parking"),
]


def rss_mb() -> int:
    return int(re.search(r"VmRSS:\s+(\d+)", open("/proc/self/status").read()).group(1)) // 1024


async def main() -> int:
    from app import rerank as rerank_mod
    from app.agents import retrieval
    from app.agents.intent import parse_intent

    base_rss = rss_mb()
    print(f"baseline RSS: {base_rss} MB\n")

    # Warm the vector client: the first query in a fresh process throws an
    # empty-message transport error (FINDINGS 6.1) and would look like a
    # reranking failure.
    from app.schemas import StructuredQuery

    try:
        await retrieval.retrieve(StructuredQuery(city="Lisbon"), limit=1)
    except Exception:
        pass

    print(f"{'query':<8} {'n':>4} {'moved@10':>9} {'inversions':>11} "
          f"{'score spread':>13} {'ms':>7}")
    print("-" * 62)

    total_moved = 0
    total_overlap = 0
    top1_changes = 0
    total_ms = 0.0
    n = 0

    for label, text in QUERIES:
        sq = await parse_intent(text)
        # Fetch the BI-ENCODER ordering. retrieve() applies reranking itself
        # when the flag is on, so asking it for candidates with RERANK_ENABLED
        # set would return an already-reranked list and every delta below would
        # be zero — which is exactly what the first run of this script showed.
        rerank_mod.settings.rerank_enabled = False
        candidates = await retrieval.retrieve(sq, limit=rerank_mod.settings.rerank_candidates)
        rerank_mod.settings.rerank_enabled = True
        if len(candidates) < 2:
            print(f"{label:<8} {'(no candidates — skipped)':>44}")
            continue

        baseline = [c.id for c, _ in candidates][:10]

        docs = [rerank_mod.listing_to_document(c, r) for c, r in candidates]
        query_text = retrieval._query_text(sq)

        t0 = time.perf_counter()
        scores = list(rerank_mod._get_encoder().rerank(query_text, docs))
        ms = (time.perf_counter() - t0) * 1000
        order = sorted(range(len(docs)), key=lambda i: scores[i], reverse=True)

        reranked = [candidates[i][0].id for i in order][:10]
        pos = {lid: i for i, lid in enumerate(baseline)}
        moved = sum(abs(pos[lid] - i) for i, lid in enumerate(reranked) if lid in pos)
        overlap = len(set(baseline) & set(reranked))

        # Inversions over the FULL candidate list, not just the top 10: if
        # reranking only shuffles ranks 30-50 that is still no user-visible
        # change, but it distinguishes "the model agrees" from "the model never
        # ran".
        inversions = sum(
            1
            for a in range(len(order))
            for b in range(a + 1, len(order))
            if order[a] > order[b]
        )
        # Score spread is the diagnostic that explains a zero delta: a tightly
        # clustered set means the candidates are near-identical in the model's
        # eyes, so there is no signal for it to exploit.
        spread = float(max(scores)) - float(min(scores))

        total_moved += moved
        total_overlap += overlap
        top1_changes += int(baseline[0] != reranked[0])
        total_ms += ms
        n += 1
        print(f"{label:<8} {len(docs):>4} {moved:>9} {inversions:>11} "
              f"{spread:>13.2f} {ms:>7.0f}")

    if n:
        print("-" * 62)
        print(f"{'mean':<8} {'':>4} {total_moved / n:>9.1f} {'':>11} "
              f"{'':>13} {total_ms / n:>7.0f}")
        print()
        print(f"  top-10 set overlap: {total_overlap / n:.1f}/10   "
              f"top-1 changed: {top1_changes}/{n}")
    print()
    print(f"RSS after reranking: {rss_mb()} MB  (+{rss_mb() - base_rss} MB over baseline)")
    print(f"model: {rerank_mod.settings.rerank_model}")
    print()
    print("Ordering only — this says how MUCH the order changed, not whether it")
    print("improved. Score the reranked output against EVAL.md's rubric for that.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
