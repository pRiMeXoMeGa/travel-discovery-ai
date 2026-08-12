# WS2 — MCP, both directions

**Why both.** Most portfolios wrap their own internal functions in MCP and call it
integration. A reviewer sees through it in seconds — why would you need a protocol to
reach your own database? Do it where the boundary is real: expose the platform to
*other* agents, and consume a *third-party* server. That is two genuine boundaries.

Estimated ~0.5 day. Highest impact per hour in the plan.

---

## Part 0 — the refactor that has to happen first (~1.5h)

`agents/orchestrator.py::_fallback_search` currently does:

```python
from ..routers.search import _build_query, _row_to_card
```

Confirmed at `orchestrator.py:63` — an agent importing private helpers from an HTTP
router, inside the function body to dodge a circular import. There is a second instance:
`routers/agents.py` imports `from .search import search as run_search` and
`from ..agents.retrieval import _parse_constraints`, so the NL-search endpoint reaches into
both a sibling router and an agent's private helper. Both should land in the service layer.

Adding six MCP tools on top of that coupling makes it permanent. Extract first:

```
app/services/
├── listings.py    search_listings, get_listing, compare_listings
├── reviews.py     synthesize_reviews          (wraps agents/review_intel)
├── availability.py check_availability          (wraps app/availability)
└── planning.py    plan_itinerary               (wraps agents/itinerary)
```

Then three callers share one layer: `routers/*` (HTTP), `agents/*` (concierge), and
`mcp_server/tools.py` (MCP). Routers become thin.

**Never let an MCP tool call your own HTTP endpoints.** Self-requests on a single-worker
Render instance risk deadlock and double the latency for no benefit. Tools call the
service layer directly.

This also cleans up `_fallback_search` — a real improvement you can point at in the
README independent of MCP.

---

## Part 1 — outbound: expose the platform as an MCP server (~2h)

### Mount, don't spawn

Mount into the existing FastAPI app at `/mcp`. One deploy, one cold start, one keep-warm
ping, and the tools share the existing asyncpg pool, Qdrant client and Redis cache. A
separate Render service would get none of that.

**The lifespan gotcha:** when you mount a FastMCP ASGI app inside FastAPI, the MCP app's
own lifespan must be chained into the parent's, or the session manager is never
initialised and every request fails with an opaque session error. `main.py` must combine
both lifespans — see `mcp_server/server.py`.

### The six tools

| Tool | Cost | Notes |
|---|---|---|
| `search_listings` | 0 LLM | semantic + filters, the workhorse |
| `get_listing_detail` | 0 LLM | gallery, amenities, aspect scores, price breakdown |
| `check_availability` | 0 LLM | deterministic availability function |
| `compare_listings` | 0 LLM | matrix only — omit the AI verdict here |
| `synthesize_reviews` | **1 LLM** | grounded, `[r#]` citations to real rows |
| `plan_itinerary` | **1 LLM** | multi-stay plan with costs |

### Four of these are not the pure wraps the table implies

Checked against the current code — budget for these or the tool contracts are fiction:

- **`compare_listings` is not 0 LLM today.** `POST /api/batch/compare` always builds the
  verdict: `_compare_verdict` fans out `review_intel.synthesize()` per listing via
  `asyncio.gather` *plus* one verdict call — up to **5 LLM calls**. The service layer needs
  a verdict-free path, or a browsing agent silently burns quota. Its `check_in`/`check_out`
  arguments are also new; `CompareRequest` accepts `listing_ids` only.
- **`synthesize_reviews` has no abstention contract.** `review_intel.synthesize()` returns
  `tuple[str, list[Citation]]`; with no reviews it returns a plain sentence and an empty
  citation list — there is no `abstained` flag. `services/reviews.py` must add it.
- **`check_availability` needs a small helper.** `is_available_range()` returns
  `(bool, total)` only; per-night rows come from `availability_window(listing_id, start,
  days, base_price)`, so the service converts a date range into `days`.
- **`search_listings.min_rating` is unenforceable on the semantic path.**
  `_build_qdrant_filter` has no rating condition, and `rating` is not among the six indexed
  payload fields — filtering on it in Qdrant Cloud strict mode returns 400 until you add
  the index and re-snapshot. Either drop the parameter, or apply it in Python after
  hydration and say so in the docstring.

Also: `City` must be **title case** (`"Amsterdam"`, `"Lisbon"`, `"Los Angeles"`). The
Qdrant payload filter is an exact `MatchValue`, so a lowercase enum matches nothing and the
tool returns an empty list with no error. The SQL path is case-insensitive
(`LOWER(city) = LOWER($1)`), which hides the bug in testing.

### What makes this more than a CRUD wrapper

`synthesize_reviews` returns **grounded synthesis with mandatory `[r#]` citations to real
review rows**, because it is backed by the existing review-intelligence agent — ranked by
Postgres full-text over 200K real reviews, and it abstains honestly when there is no
evidence. An external agent calling it gets citations it can verify, not a summary the
model invented.

Say exactly that in the tool description. Which brings us to:

### Tool docstrings are read by a model, not a human

The docstring becomes the tool description in the MCP schema. It is the only thing a
calling agent sees when deciding whether to call your tool. Write for that reader:

- State what grounding the tool guarantees ("citations reference real review rows")
- State the cost ("this tool performs an LLM call; prefer `search_listings` for browsing")
- State the failure mode ("returns `abstained: true` when no reviews match")
- Name real enum values — `room_type` accepts the actual Inside Airbnb values

Most people write these for humans and get poor tool selection as a result. This is the
cheapest quality win in WS2.

### Auth and rate limiting

A public MCP server on a free tier, where two tools cost Gemini calls, is a quota leak
waiting to happen.

Wrap the mounted app in a small **ASGI middleware** checking `Authorization: Bearer
<MCP_API_KEY>` — transport-agnostic and immune to fastmcp's auth API changing between
versions. Add a per-key RPM cap on the two LLM-backed tools only.

Set `MCP_API_KEY` in the Render dashboard. Never commit it. Rotate it before you share
the repo publicly.

### Deliverable — the screenshot

Connect **Claude Desktop** to `https://travel-discovery-api.onrender.com/mcp` as a custom
connector (Settings → Connectors → add custom connector; check current docs, the UI moves).
Then ask it to plan a Lisbon trip and screenshot it calling your tools and returning cited
review evidence.

Fallbacks if the connector path gives trouble: `mcp-remote` as a stdio bridge in
`claude_desktop_config.json`, or MCP Inspector pointed at the same URL. Inspector always
works — but the Claude Desktop screenshot is far more persuasive, so try that first.

Save to `version2/img/mcp-claude-desktop.png` and put it near the top of the README.

**Warm the instance before you screenshot.** Render free tier cold-starts in 40–50s and
some clients time out first.

---

## Part 2 — inbound: consume a weather MCP server (~1.5h)

### Why weather is the honest choice

A trip planner that checks the forecast for the actual travel dates is a genuinely better
planner. It is not a framework demo bolted on — it changes the output. Open-Meteo needs no
API key, so there is no credential story to explain.

### Run it yourself

Do not depend on a stranger's hosted endpoint for a demo you'll show in an interview. Use
a published third-party image and deploy it as a second Render service:

- `cmer81/open-meteo-mcp` — `TRANSPORT=http`, supports `API_KEY` and `RATE_LIMIT_RPM`
- `isdaniel/mcp_weather_server` — image `dog830228/mcp_weather_server`,
  `--mode streamable-http`

This is still consuming a third-party MCP server, which is what proves client capability.
You just control the uptime. Add it to `docker-compose.yml` for local dev too.

### ⚠ Two keep-warm Render services do not fit the free tier

Render's free plan gives **750 instance-hours per month across the account** — roughly
*one* service kept near-always-on (~730 h). Keep-warm pinging both the API and a weather
service is ~1460 h and will exhaust the allowance partway through the month, taking the
**main API down with it**. The v1 plan already recorded this ("750 free hrs/mo ≈ one
near-always-on service").

Pick one, deliberately, and write it in the README:

1. **Ping only the API; let weather cold-start.** With a 3s timeout the first call after
   idle always fails — which is fine, because degradation is silent by design and the demo
   still works. Cheapest, and it exercises the failure path honestly.
2. **Run weather locally / in docker-compose only**, and demo the inbound direction on the
   local stack. The Claude Desktop screenshot still comes from the deployed outbound server.
3. **Skip the second service**: call Open-Meteo's plain HTTP API from the itinerary agent
   and drop the inbound-MCP claim. Cheapest to run, but it loses the "MCP client" half of
   the story, which is the point of WS2.

Option 1 was the recommendation; **option 2 is what shipped** (2026-08-11). The deciding
detail is that option 1's "let weather cold-start" still leaves the first call after idle
timing out — so in practice the deployed behaviour would be the degradation path most of
the time, which is exactly what option 2 gives for free. The inbound direction is therefore
demonstrated on the local stack, and the outbound server is what gets deployed.

Do **not** put a second cron ping on any weather service without checking the month's usage.

### Where it hooks

Inside `agents/itinerary.py::plan_itinerary`, after the segment structure is decided and
dates are known. One call per plan, not per stay.

Feed the forecast into `plan["notes"]`, which the answer prompt already surfaces:

> *"Rain forecast for your Lisbon segment on the 14th–15th — Alfama has more covered
> options."*

That is the demo moment: an external tool visibly changing the recommendation.

### Client discipline

- **One client, created lazily, reused.** Not per request.
- **3-second hard timeout.** Weather is an enhancement.
- **Full graceful degradation** — on timeout or error, skip the note and continue. Same
  contract every agent in `orchestrator.py` already honours. A third-party outage must
  never break a trip plan.
- **Cache by (city, date-range) in Redis**, 6h TTL, via the existing `cache.py`. Forecasts
  do not change minute to minute and this removes most calls.
- Emit an `AgentStep("weather_mcp", ...)` so the tool call is **visible in the SSE trace**.
  An invisible integration is indistinguishable from no integration — same principle as
  the memory panel. Note the frontend needs a matching entry: `STEP_LABEL` in
  `ConciergePanel.tsx` has no `weather_mcp` key, so the trail would render the raw string.
  (Same gap as the `memory` step in WS1 — fix both together.)

### Cost

Zero Gemini calls. It is a tool call, not a completion.

---

## Acceptance

- [ ] `app/services/` extracted; `_fallback_search` no longer imports from routers
- [ ] Six tools live at `/mcp`, schemas visible in MCP Inspector
- [ ] Bearer auth enforced; unauthenticated request returns 401
- [ ] Claude Desktop screenshot in `version2/img/`
- [ ] `synthesize_reviews` returns real `[r#]` citations through MCP
- [x] Weather MCP consumed by the itinerary agent — **option 2 taken (2026-08-11):
      local/docker-compose only, NOT deployed and NOT keep-warm pinged.** A second
      free service could not be kept warm on 750 account-hours, so every first call
      after idle would time out and return nothing anyway — deploying it would buy a
      worse version of the degradation path that already runs. `WEATHER_MCP_URL` stays
      declared in `render.yaml` so the choice is reversible without a code change.
- [ ] Weather failure degrades silently — verified by stopping the weather service
- [ ] `weather_mcp` step appears in the SSE trace
- [ ] README: "MCP — both directions" section; `JD_MAPPING.md` row for section 1

## Interview lines

- "The platform is an MCP client *and* an MCP server. Here's Claude Desktop booking
  through it."
- "Tool descriptions are written for the calling model, not for a human reader —
  they declare grounding guarantees, cost, and abstention behaviour."
- "`synthesize_reviews` returns citations to real review rows over 200K reviews. An agent
  calling it can verify every claim."
- "The weather integration degrades silently. A third-party outage can't break a trip plan."
