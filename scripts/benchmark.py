#!/usr/bin/env python
"""Model benchmark harness (WS5) — cost, latency, accuracy across models.

    docker compose exec -T -e GEMINI_API_KEY=... backend python - < scripts/benchmark.py
    ... --models gemini-3.1-flash-lite,gemini-2.5-flash --repeats 2

DESIGN: no LLM judge. Every metric here is checkable against something the
system already knows to be true, because a model scoring another model's output
gives you a number that moves when the judge changes and cannot be reproduced by
anyone else. Three stages, each with a mechanical ground truth:

  1. INTENT — field-level precision/recall/F1 against hand-written expected
     values. The expected structured query for "an entire place in Lisbon under
     130" is not a matter of opinion.

  2. CITATION VALIDITY — every `[rN]` label in the synthesis prose must resolve
     to a citation the same call returned, and every citation id must be a real
     `reviews.id`. Both are set-membership checks against the database.

  3. ANSWER ENTITY CONTAINMENT — every property name the answer mentions must
     appear in the grounded context it was given. This catches the failure that
     matters most (an invented listing) without asking anyone's opinion.

COST is derived from MEASURED tokens (`usage_source: "measured"`, WS0-D) times a
price table — never from an estimate. The table is now set from Google's
published rates (checked 2026-08-13) rather than placeholders, and is printed
with every run so a stale number stays visible. Re-check before quoting: the
original placeholders understated Flash-Lite by ~3x and turned a 1.4x cost gap
into a claimed 4x one.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/backend")

# USD per 1M tokens. Gemini rates from https://ai.google.dev/gemini-api/docs/pricing
# (paid tier, text input), checked 2026-08-13. Printed with every run so a stale
# number is visible rather than silently propagated into a README.
#
# These replace placeholders that were 2.5x low on Flash-Lite input and 3.75x low
# on its output. The published EVAL figures derived from them were wrong in a way
# that flattered the recommendation, so treat this table as load-bearing.
PRICES = {
    "gemini-3.1-flash-lite": {"in": 0.25, "out": 1.50},
    "gemini-2.5-flash": {"in": 0.30, "out": 2.50},
    # ⚠ Anthropic rate NOT re-verified — this model has never been run here
    # (no ANTHROPIC_API_KEY), so the row is absent from results either way.
    "claude-haiku-4-5-20251001": {"in": 1.00, "out": 5.00},
}

# Stage 1 fixtures: query -> the fields a correct parse must produce.
# Only fields with an unambiguous right answer are scored; "vibe" and
# soft_preferences are deliberately excluded as matters of taste.
INTENT_CASES = [
    ("an entire place in Lisbon under 130 with a balcony",
     {"city": "Lisbon", "budget_per_night": 130.0}),
    ("family-friendly place in Amsterdam with a pool and kitchen under 250",
     {"city": "Amsterdam", "budget_per_night": 250.0}),
    ("Plan a 4-night Los Angeles trip for a couple, budget $1200 total",
     {"city": "Los Angeles", "party_size": 2, "budget_total": 1200.0}),
    ("3 nights in Amsterdam for 2 adults under 200 a night",
     {"city": "Amsterdam", "party_size": 2, "budget_per_night": 200.0}),
    ("a shared room in Lisbon, cheapest possible",
     {"city": "Lisbon"}),
]

REVIEW_CASES = ["Lisbon", "Amsterdam"]
ANSWER_CASES = [
    "a quiet flat in Lisbon with a balcony",
    "an entire place in Amsterdam near the centre",
]

_CITE = re.compile(r"\[r(\d+)\]")
# Answers decorate property names — "Amsterdam city center (Centrum-West):" for
# a listing cited as "Amsterdam city center". Normalise before comparing, or a
# correctly-grounded mention is scored as a hallucination. An early version of
# this harness did exactly that and reported 56% containment for a model that
# was in fact 100% grounded; the number was a metric artifact, not a finding.
_PARENTHETICAL = re.compile(r"\([^)]*\)")
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")


def _norm_name(text: str) -> str:
    t = _PARENTHETICAL.sub(" ", (text or "").lower())
    t = _NON_ALNUM.sub(" ", t)
    return " ".join(t.split())


def _names_match(a: str, b: str) -> bool:
    """Grounded if either name contains the other's leading words.

    Directional containment matters: the answer may abbreviate the listing name
    OR extend it, and only checking one direction misses half the real matches.
    """
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    head = " ".join(a.split()[:4])
    return bool(head) and head in b



def f1(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


async def bench_intent(model: str) -> dict:
    """Field-level P/R/F1 over the fixtures above."""
    from app.agents import intent
    from app.observability import AgentStep

    tp = fp = fn = 0
    lat: list[float] = []
    tokens_in = tokens_out = 0

    for query, expected in INTENT_CASES:
        step = AgentStep("intent", "start")
        t0 = time.perf_counter()
        # `parse_intent` takes no model argument — run_model() sets
        # settings.gemini_model, which is what every llm.py call resolves
        # against. Passing a model here would be a no-op that looked deliberate.
        sq = await intent.parse_intent(query, step=step)
        lat.append((time.perf_counter() - t0) * 1000)
        tokens_in += getattr(step, "input_tokens", 0) or 0
        tokens_out += getattr(step, "output_tokens", 0) or 0

        got = sq.model_dump(mode="json")
        for field, want in expected.items():
            have = got.get(field)
            if have is None:
                fn += 1                      # required field missed
            elif str(have) == str(want) or have == want:
                tp += 1
            else:
                fp += 1                      # present but wrong
    p, r, f = f1(tp, fp, fn)
    return {"precision": p, "recall": r, "f1": f, "lat": lat,
            "tin": tokens_in, "tout": tokens_out, "n": len(INTENT_CASES)}


async def bench_citations(model: str) -> dict:
    """Every [rN] resolves to a returned citation; every citation is a real row."""
    from app.agents import retrieval
    from app.db import get_pool
    from app.schemas import StructuredQuery
    from app.services import reviews as reviews_service

    labelled = resolved = 0
    cited = real = 0
    lat: list[float] = []
    abstained = 0

    for city in REVIEW_CASES:
        cands = await retrieval.retrieve(StructuredQuery(city=city), limit=3)
        ids = [c.id for c, _ in cands]
        if not ids:
            continue
        t0 = time.perf_counter()
        out = await reviews_service.synthesize_reviews(ids)
        lat.append((time.perf_counter() - t0) * 1000)
        if out.get("abstained"):
            abstained += 1
            continue

        citations = out.get("citations") or []
        cite_ids = [getattr(c, "id", None) or (c.get("id") if isinstance(c, dict) else None)
                    for c in citations]
        labels = {int(m) for m in _CITE.findall(out.get("text") or "")}
        labelled += len(labels)
        resolved += sum(1 for n in labels if 1 <= n <= len(cite_ids))

        # Set-membership against the database — the strongest available check.
        pool = await get_pool()
        async with pool.acquire() as con:
            rows = await con.fetch(
                "SELECT id::text FROM reviews WHERE id::text = ANY($1::text[])",
                [str(i) for i in cite_ids if i],
            )
        cited += len(cite_ids)
        real += len(rows)

    return {
        "label_resolution": resolved / labelled if labelled else None,
        "citation_validity": real / cited if cited else None,
        "abstained": abstained, "lat": lat, "n": len(REVIEW_CASES),
    }


async def bench_answer(model: str) -> dict:
    """Every listing name in the answer must appear in the grounded context."""
    from app.agents import orchestrator
    from app.schemas import ConciergeRequest

    total = grounded = 0
    lat: list[float] = []
    tin = tout = 0
    measured = 0
    runs = 0

    for query in ANSWER_CASES:
        answer_parts: list[str] = []
        names: list[str] = []
        trace = None
        t0 = time.perf_counter()
        async for ev in orchestrator.run_concierge(ConciergeRequest(query=query)):
            if ev.get("type") == "token":
                answer_parts.append(ev.get("text", ""))
            elif ev.get("type") == "data":
                names = [
                    (c.get("snippet") or "")
                    for c in (ev.get("citations") or [])
                    if c.get("kind") == "listing"
                ]
            elif ev.get("type") == "done":
                trace = ev.get("trace") or {}
        lat.append((time.perf_counter() - t0) * 1000)
        runs += 1
        if trace:
            tin += trace.get("input_tokens") or 0
            tout += trace.get("output_tokens") or 0

        answer = "".join(answer_parts)
        # Bolded names are how this app's answers refer to properties.
        mentioned = re.findall(r"\*\*([^*]{4,60})\*\*", answer)
        cites = [_norm_name(n) for n in names if n]
        for m in mentioned:
            norm = _norm_name(m)
            if not norm:
                continue
            total += 1
            if any(_names_match(norm, c) for c in cites):
                grounded += 1

    return {
        "entity_containment": grounded / total if total else None,
        "mentions": total, "lat": lat, "tin": tin, "tout": tout, "runs": runs,
        "measured": measured,
    }


def cost_usd(model: str, tin: int, tout: int) -> float | None:
    p = PRICES.get(model)
    if not p:
        return None
    return (tin / 1e6) * p["in"] + (tout / 1e6) * p["out"]


def pct(v):
    return "n/a" if v is None else f"{v * 100:.0f}%"


def p50p95(lat: list[float]) -> tuple[float, float]:
    if not lat:
        return (0.0, 0.0)
    s = sorted(lat)
    return (statistics.median(s), s[min(len(s) - 1, int(0.95 * len(s)))])


async def run_model(model: str) -> dict:
    from app.config import settings

    previous = settings.gemini_model
    settings.gemini_model = model          # the answer/synthesis path reads this
    try:
        intent_r = await bench_intent(model)
        cite_r = await bench_citations(model)
        ans_r = await bench_answer(model)
    finally:
        settings.gemini_model = previous

    lat_all = intent_r["lat"] + cite_r["lat"] + ans_r["lat"]
    tin = intent_r["tin"] + ans_r["tin"]
    tout = intent_r["tout"] + ans_r["tout"]
    p50, p95 = p50p95(ans_r["lat"])
    return {
        "model": model,
        "intent_f1": intent_r["f1"],
        "intent_p": intent_r["precision"],
        "intent_r": intent_r["recall"],
        "label_resolution": cite_r["label_resolution"],
        "citation_validity": cite_r["citation_validity"],
        "entity_containment": ans_r["entity_containment"],
        "mentions": ans_r["mentions"],
        "answer_p50": p50,
        "answer_p95": p95,
        "tin": tin, "tout": tout,
        "cost": cost_usd(model, tin, tout),
        "turns": ans_r["runs"],
        # Per STAGE, because the two are measured over different numbers of
        # runs. Dividing the combined total by answer turns alone charges every
        # intent fixture against a turn, so the "cost per turn" moved whenever
        # someone added a fixture — an artifact of the harness, not the system.
        "intent_cost": (
            (cost_usd(model, intent_r["tin"], intent_r["tout"]) or 0) / intent_r["n"]
            if intent_r["n"] else None
        ),
        "turn_cost": (
            (cost_usd(model, ans_r["tin"], ans_r["tout"]) or 0) / ans_r["runs"]
            if ans_r["runs"] else None
        ),
        "lat_n": len(lat_all),
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="gemini-3.1-flash-lite,gemini-2.5-flash")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    # Warm the vector client: the first query in a fresh process throws an
    # empty-message transport error and would be scored as a model failure.
    from app.agents import retrieval
    from app.schemas import StructuredQuery

    try:
        await retrieval.retrieve(StructuredQuery(city="Lisbon"), limit=1)
    except Exception as exc:  # noqa: BLE001 - warm-up only
        print(f"  (warm-up query failed, continuing: {type(exc).__name__})", file=sys.stderr)

    rows = []
    for m in models:
        print(f"running {m} …", file=sys.stderr)
        try:
            rows.append(await run_model(m))
        except Exception as exc:  # noqa: BLE001
            print(f"  {m} FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    print()
    print(f"{'model':<26} {'intent F1':>9} {'cite ok':>8} {'entity':>7} "
          f"{'p50 ms':>7} {'p95 ms':>7} {'$/intent':>9} {'$/turn':>9}")
    print("-" * 96)
    for r in rows:
        ic, tc = r.get("intent_cost"), r.get("turn_cost")
        print(f"{r['model']:<26} {r['intent_f1'] * 100:>8.0f}% "
              f"{pct(r['citation_validity']):>8} {pct(r['entity_containment']):>7} "
              f"{r['answer_p50']:>7.0f} {r['answer_p95']:>7.0f} "
              f"{(f'${ic:.5f}') if ic else 'n/a':>9} "
              f"{(f'${tc:.5f}') if tc else 'n/a':>9}")
    print()
    print("intent F1        field-level, against hand-written expected values")
    print("cite ok          share of returned citations that are real reviews.id rows")
    print("entity           share of property names in the answer found in its context")
    print("$/intent         one NL-search parse: intent-stage tokens / intent cases")
    print("$/turn           one full concierge turn: answer-stage tokens / turns")
    print("                 (these are PER STAGE. An earlier version summed both")
    print("                  stages and divided by turns alone, which inflated the")
    print("                  headline cost and moved it whenever a fixture was added)")
    print("                 MEASURED tokens x the price table in this file -")
    print("                 verify against current published rates before quoting")
    for m, pr in PRICES.items():
        print(f"                 {m}: ${pr['in']}/1M in, ${pr['out']}/1M out")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
