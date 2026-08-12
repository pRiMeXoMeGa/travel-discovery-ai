#!/usr/bin/env python
"""Verify a running MCP endpoint — local or deployed — end to end.

Checks, in order:
  1. unauthenticated POST is rejected (401, not 503 and not 200)
  2. a wrong bearer token is rejected
  3. the real token completes an MCP session and lists all six tools
  4. `search_listings` returns real rows (0 LLM calls)
  5. `synthesize_reviews` returns citations to real review rows (1 LLM call)

The key is read from the environment and never printed. Step 5 spends one
Gemini call; pass --no-llm to stop after step 4.

    # local
    MCP_API_KEY=... python scripts/verify_mcp.py

    # deployed — warm it first, Render free tier cold-starts in 40-50s
    MCP_API_KEY=... python scripts/verify_mcp.py \\
        --url https://travel-discovery-api.onrender.com/mcp/

Needs `fastmcp` and `httpx`, which the backend image already has:

    docker compose exec -T -e MCP_API_KEY backend python - < scripts/verify_mcp.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

DEFAULT_URL = "http://localhost:8000/mcp/"
EXPECTED_TOOLS = {
    "search_listings",
    "get_listing_detail",
    "check_availability",
    "compare_listings",
    "synthesize_reviews",
    "plan_itinerary",
}

ok = "  OK   "
bad = "  FAIL "


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.environ.get("MCP_URL", DEFAULT_URL))
    parser.add_argument("--no-llm", action="store_true",
                        help="skip synthesize_reviews (saves one Gemini call)")
    args = parser.parse_args()

    key = os.environ.get("MCP_API_KEY", "").strip()
    if not key:
        print("!! set MCP_API_KEY in the environment", file=sys.stderr)
        return 2

    url = args.url if args.url.endswith("/") else args.url + "/"
    # The trailing slash is not cosmetic: the mount redirects /mcp -> /mcp/
    # with a 307, and not every MCP client follows redirects on POST.

    import httpx
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    failures = 0
    probe = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}

    print(f"target: {url}\n")

    # ── 1 + 2) auth is enforced ──────────────────────────────────────────────
    async with httpx.AsyncClient(timeout=60.0) as http:
        for label, hdrs in (
            ("unauthenticated rejected", headers),
            ("wrong token rejected", {**headers, "Authorization": "Bearer wrong"}),
        ):
            try:
                r = await http.post(url, headers=hdrs, json=probe)
            except Exception as exc:  # noqa: BLE001
                print(f"{bad}{label} — could not connect: {exc}")
                return 2
            if r.status_code == 401:
                print(f"{ok}{label} (401)")
            elif r.status_code == 503:
                failures += 1
                print(f"{bad}{label} — got 503: MCP_API_KEY is NOT set on the "
                      "server. Auth fails closed, so /mcp is dead, not open.")
            elif r.status_code == 307:
                failures += 1
                print(f"{bad}{label} — got 307: use a trailing slash on /mcp/")
            else:
                failures += 1
                print(f"{bad}{label} — expected 401, got {r.status_code}")

    # ── 3) real session, all six tools ───────────────────────────────────────
    transport = StreamableHttpTransport(url, headers={"Authorization": f"Bearer {key}"})
    try:
        async with Client(transport) as client:
            tools = {t.name for t in await client.list_tools()}
            missing = EXPECTED_TOOLS - tools
            extra = tools - EXPECTED_TOOLS
            if missing:
                failures += 1
                print(f"{bad}tools/list — missing {sorted(missing)}")
            else:
                print(f"{ok}tools/list — all six present"
                      + (f" (plus {sorted(extra)})" if extra else ""))

            # ── 4) search_listings, 0 LLM ────────────────────────────────────
            result = await client.call_tool(
                "search_listings", {"city": "Lisbon", "price_max": 130, "limit": 3}
            )
            payload = result.structured_content or json.loads(result.content[0].text)
            rows = payload.get("results") or payload.get("listings") or []
            if rows:
                print(f"{ok}search_listings — {len(rows)} rows, "
                      f"first: {str(rows[0].get('name'))[:44]}")
            else:
                failures += 1
                print(f"{bad}search_listings — no rows returned")

            # ── 5) synthesize_reviews, 1 LLM ─────────────────────────────────
            if args.no_llm:
                print("  SKIP synthesize_reviews (--no-llm)")
            elif rows:
                result = await client.call_tool(
                    "synthesize_reviews", {"listing_id": rows[0]["id"]}
                )
                payload = result.structured_content or json.loads(result.content[0].text)
                citations = payload.get("citations") or []
                if payload.get("abstained"):
                    # Not a failure: abstaining honestly when a listing has no
                    # reviews is the documented contract.
                    print(f"  NOTE synthesize_reviews abstained "
                          f"(reason={payload.get('reason')}) — valid, but pick a "
                          "listing with reviews for the demo")
                elif citations:
                    print(f"{ok}synthesize_reviews — {len(citations)} citations "
                          "to real review rows")
                else:
                    failures += 1
                    print(f"{bad}synthesize_reviews — no citations and not abstained")
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(f"{bad}MCP session failed: {type(exc).__name__}: {exc}")

    print()
    print("FAILED" if failures else "All checks passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
