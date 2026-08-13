# Backend (FastAPI)

This is the API service. It exposes both the traditional search/filter endpoints and the streaming multi-agent concierge.

Status: implemented and verified (Phases 2 and 3). Every endpoint below is live, and the 4-agent concierge streams over SSE. The batch-compare AI verdict (parallel per-listing review synthesis plus a grounded LLM verdict) is in too; if the LLM call fails it just falls back to a matrix-only response.

## Layout

```
app/
├── main.py            # FastAPI app: CORS, lifespan, router includes, /health
├── config.py          # pydantic-settings loaded from env (.env)
├── db.py              # async Postgres pool (asyncpg)
├── vectorstore.py     # Qdrant async client
├── cache.py           # Redis client + cache_get/cache_set helpers
├── embeddings.py      # query-time embeddings (fastembed/ONNX bge-small, 384-dim)
├── llm.py             # provider abstraction: Gemini (default) / Anthropic, streaming + structured output
├── observability.py   # per-request token/latency/agent-step trace
├── schemas.py         # Pydantic models (SearchFilters, ListingCard, StructuredQuery, …)
├── routers/
│   ├── search.py      # POST /api/search
│   ├── listings.py    # GET /api/listings/{id}, /reviews, POST /api/batch/compare
│   ├── agents.py      # POST /api/concierge/stream (SSE), POST /api/nl-search
│   └── memory.py      # DELETE /api/memory/{id}  (WS1 forget button)
└── agents/
    ├── orchestrator.py  # coordinates the 4 agents, yields step events for streaming
    ├── intent.py        # NL -> StructuredQuery
    ├── retrieval.py     # semantic (listings + summary vectors, RRF-fused) + hard-filtered
    │                    # + neighbourhood-level area constraints; ranked + grounded rationale
    ├── review_intel.py  # grounded review synthesis with citations
    └── itinerary.py     # multi-day, multi-property plans
planner/                 # WS3 LangGraph trip planner
├── graph.py             # replan cycle, HITL interrupt(), conditional routing
└── checkpointer.py      # BaseCheckpointSaver over the existing Redis
rerank.py                # WS4 cross-encoder — lazy, flag-gated, OFF by default
memory/                  # WS1 traveller + trip memory (mem0)
├── store.py             # the ONLY entry point — wraps every mem0 call in
│                        # asyncio.to_thread; never raises, returns empty on failure
└── fastembed_embedder.py  # local 384-dim embedder so recall costs zero API calls
```

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/health` | Liveness (also the keep-warm ping target) |
| `POST` | `/api/search` | Filtered/sorted search with calendar availability |
| `GET`  | `/api/listings/{id}` | Property detail (gallery, amenities, aspect scores, summary + `summary_provenance`, price breakdown) |
| `GET`  | `/api/listings/{id}/reviews` | Reviews filtered by language / score / topic |
| `POST` | `/api/batch/compare` | Compare 2-4 listings (parallel review synthesis for the AI verdict) |
| `POST` | `/api/concierge/stream` | Multi-agent concierge over SSE; streams intermediate steps + answer tokens |
| `POST` | `/api/nl-search` | Parse NL into structured filters for the search bar / chips |
| `DELETE` | `/api/memory/{id}` | Forget one remembered item (backs the memory panel) |
| `POST` | `/api/planner/stream` | WS3 LangGraph planner; ends at `done` or `awaiting_input` |
| `POST` | `/api/planner/resume` | Continue an interrupted plan (approve / adjust) |
| — | `/mcp` | WS2 MCP server, six tools, bearer auth |

Interactive docs at `/docs` when it's running.

## Run

Normally the root `docker compose up --build` starts it. Standalone:

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000   # needs a .env at repo root (see ../.env.example)
```

## Agents (`app/agents/`)

| Agent | Role | Grounding |
|---|---|---|
| `intent` | Turns NL into a `StructuredQuery` via a structured-JSON LLM call; resolves vague dates like "late June" to ISO ranges | Leaves out fields it can't determine, so it doesn't fabricate |
| `retrieval` | Embeds the intent (fastembed), runs a Qdrant `listings` search fused with hard-constraint payload filters (including the real `room_type` values), then hydrates the rows from Postgres | The per-result rationale is built deterministically from real fields, so the LLM can't invent attributes |
| `review_intel` | Ranks a property's reviews with Postgres full-text (focus-aware `to_tsvector('simple')` + `idx_reviews_fts`), with a balanced top/bottom-rated fallback, then has the LLM synthesize ("praise X, complain about Y") | Citations to real review rows (`[r#]`) are mandatory, and it abstains honestly when there are no reviews |
| `itinerary` | The LLM only decides the segment structure; the property selection and costing are deterministic, driven by the availability function | Totals come from real prices, the budget is checked, and each stay has ranked swap-out alternatives |

`orchestrator.py` is a custom async generator. It routes by intent, wraps each agent in a guard that emits `status:"error"` and falls back to traditional filtered search instead of crashing the stream, and records per-step token/latency through `observability.py`.

## Vector layout (Option A, real data)

Qdrant holds `listings` (50K) and `summaries` (50K per-property review summaries), both at 384-dim. Reviews are not vector-embedded: all 200K live in Postgres behind a GIN full-text index (`idx_reviews_fts`). This was a deliberate call (the 4-core CPU couldn't embed 200K long reviews in any reasonable time), so review search comes from Postgres full-text. Per-property review retrieval is a fast indexed `listing_id` slice, so there's no latency cost; what you give up is semantic recall, which the summary vectors and the LLM reading the real rows make up for. There's more on this in the root README's "Key trade-offs".

**Summary provenance (WS0-A).** `listing_summaries.provenance` is `'llm'` only for rows written by `scripts/backfill_summaries.py`; the ingest default is `'heuristic'`, which is two review quotes truncated at ~120 chars. It is surfaced as `ListingDetail.summary_provenance` because the frontend gates its "AI Review Summary" heading on it — without the flag the UI would claim all 50K summaries are model-written. The backfill re-embeds the rewritten rows into the `summaries` collection using `UUID(listing_id).int >> 64`, the same point id ingestion uses, so the upsert replaces rather than duplicates. Two operational notes: listing detail is cached in Redis for 300s, so entries written before this shipped return `summary_provenance: null` until the TTL expires (harmless — the UI treats anything that is not `'llm'` as heuristic, so it degrades to the honest label); and the run writes Postgres and Qdrant separately, so `summary_embedded_at < updated_at` marks rows whose vector is stale, repairable with `--reembed-only`.

## Notes and decisions

- Review search runs on Postgres full-text, not vectors. `review_intel._retrieve_review_snippets` uses `to_tsvector('simple', text) @@ plainto_tsquery(...)` ranked by `ts_rank`, scoped to the property's `listing_id` (the 'simple' config is multilingual-safe). When there's no focus or no match, it falls back to the top and bottom-rated reviews.
- I call the LLM over REST rather than the deprecated `google-generativeai` SDK. `llm.py` hits Gemini's `generateContent` / `streamGenerateContent` via httpx, with structured JSON (`responseMimeType`), retry-on-429/5xx with backoff, and a one-shot JSON repair pass. Switching provider (`gemini` or `anthropic`) lives behind that one module.
- Custom orchestrator instead of LangGraph/CrewAI. I wanted first-class SSE step streaming and exact token/latency accounting, and for 4 cooperating agents that's lighter to hand-roll.
- fastembed (ONNX) instead of sentence-transformers/torch, so it fits Render's free 512 MB. Same 384-dim `bge-small-en-v1.5` at ingest and query time, so the vectors share one space.
- SSE instead of WebSocket. It works through Render/Vercel over HTTPS and just needs a long-lived host (not serverless).
- Async throughout: asyncpg pool, async Qdrant and Redis, and the CPU-bound embedding goes through `asyncio.to_thread`. Redis caching (search, retrievals, review syntheses) degrades gracefully if Redis is down.

## Trade-offs and simplifications

- Beds as capacity. There's no `max_guests` column, so guest filtering uses `beds`.
- The availability filter runs after DB pagination, so the search `total` reflects the pre-availability count. Fine at this scale.
- `app/availability.py` mirrors `ingestion/availability.py` (same hash, same params), so keep the two in sync.
- Batch-compare AI verdict: both the matrix (price/amenities/rating/calendar) and the LLM verdict (parallel per-listing review synthesis plus one grounded verdict, cached by the listing set) are implemented. The verdict drops to null (matrix only) if the LLM call fails.
- Qdrant's `.search` throws a deprecation warning under client 1.12. It still works; the `query_points` migration is deferred.


## Memory (WS1)

The concierge remembers the traveller between sessions. Two scopes: **traveller**
(`user_id`) for standing preferences, and **trip** (`trip::<id>`) for the current
journey. Both are written from a **single** extraction — one inferred `add()` into the
traveller scope, then the already-extracted facts mirrored into the trip scope with
`infer=False` (zero extra LLM calls). Extracting twice would cost double *and* be
non-deterministic: two runs over the same turn can disagree, so the scopes would
silently diverge.

Identity is a **localStorage UUID, not authentication** — same-browser persistence
only. Clear site data or switch device and you are a new traveller. Anyone holding the
id can read those memories, so nothing sensitive should be stored under it. The
`DELETE` endpoint is deliberately not authorization-bearing and says so in its docstring.

Four hooks in `orchestrator.run_concierge`, all on the existing SSE vocabulary (no new
event type — the panel filters on `agent == "memory"` and switches on `data.phase`):

1. **recall** before intent — zero LLM calls (local embed + two Qdrant reads)
2. remembered context into the **intent** prompt — same call count as before
3. validated **dealbreakers** into retrieval as hard payload filters
4. **write** after the answer has finished streaming

### Dealbreakers are filters, not hints

"Never show me shared rooms" becomes a Qdrant `must_not` condition, not a sentence in a
prompt the model may ignore. That only works because polarity is captured at **write**
time, by the intent call, while the sentence is still visible:

- polarity is not a property of the field — `pets_allowed` means *pets are permitted*,
  so an allergy sufferer needs `must_not` on the field a dog owner needs `must` on;
- scanning remembered text for vocabulary terms misfires on *"the elevator was broken,
  avoid this place"*, which reads as **require elevator**.

Read-time projection (`extract_dealbreakers`) is therefore pure, deterministic and free.

Rules are also **fetched by kind, never ranked against the query**. Preferences come from
a similarity `search()`; standing rules come from `get_all(filters={"kind":
"dealbreaker"})`. That split is not cosmetic — measured with one dealbreaker plus ten
unrelated memories, the rule fell outside the top-6 similarity window for **3 of 3**
ordinary queries, so the hard filter silently stopped applying. A guarantee cannot be
gated on an embedding coin flip.
Rules are validated against the closed vocabulary (the 18 amenities and four real room
types) and **fail closed**: anything unenforceable is recorded as a soft preference and
badged as such in the panel, because a rule the traveller believes is enforced but isn't
is worse than not having the feature.

All three routes apply dealbreakers — `search`, `review` and `itinerary`. The planner
threads `exclude` into its single retrieval call, which produces both the chosen stay and
its swap-out alternatives, so a traveller cannot click a swap and land on exactly the
thing they banned.

Rules can be revoked in conversation as well as from the panel: `suppress_dealbreakers`
("actually, shared rooms are fine now") deletes the matching stored rules before the
turn's write. Matching is word-boundary based against the rule's `field`/`value`
metadata, never a loose substring — deleting a rule the traveller did *not* ask to remove
silently un-enforces a guarantee, which is the worse error.

### Safety

- **Override path.** If saved rules empty the result set, the search is retried without
  them and the answer is told to say so — a standing rule that silently returns nothing
  is worse than no rule.
- **Disclosure.** `_ANSWER_SYSTEM` requires the answer to state when saved preferences
  shaped the results, and when they had to be relaxed.
- **Injection defence.** Remembered text is attacker-influenced and persistent, so it
  goes in the *user* message inside a delimited `MEMORY-DATA` block labelled as data,
  with the delimiter stripped from the content and a 2000-char cap.
- **No hard filters from memory.** `city`, dates and budget become Qdrant/SQL
  conditions, so a leaked "Lisbon" would make every other city silently unsearchable.
  A post-call check drops any of those five fields whose value traces to the memory
  block but not to this turn's request.

### Cost and footprint

Recall is free (local embeddings) and the trip-state write is free (`infer=False`). The
memory write adds exactly **one** Gemini call.

Measured per turn, counting both `app/llm.py` and mem0's own calls (mem0 bypasses
`llm.py` entirely, so it is invisible to the trace and has to be counted separately):

| Route | app/llm.py | mem0 | total |
|---|---|---|---|
| `search` | 2 | 1 | **3** |
| `search` + active trip | 2 | 1 | **3** |
| `review` | 3 | 1 | **4** |
| `itinerary` | 3 | 1 | **4** |
| composite `search`+`review` (EVAL Q2) | 3 | 1 | **4** |

**The ≤4 ceiling holds.** An earlier estimate of 5 assumed mem0 spends 1–2 calls on
extraction plus an update decision; measured across four consecutive turns for the same
traveller — including turns with accumulated memories to reconcile — it consistently
spends **1**. Composite routing costs nothing extra either: the `search` route runner
makes no LLM call of its own.

Write latency is 3.8–7.9s, so the wait is capped at 15s; the cap bounds how long we
*wait*, not the write, which completes regardless.

Backend RSS with everything exercised (concierge, memory, MCP, LangGraph planner):
**479 MB peak against Render's 512 MB** — 33 MB of headroom, which is why WS4's
cross-encoder ships disabled (it costs a further +156 MB). Progression: idle 193 MB →
+concierge 406 → +memory 409 → +planner 479.

Both the query embedder and mem0's BM25 sparse encoder are **baked into the image**.
Fetching them lazily cost ~55s on the first request and overran the memory write cap on
a cold instance.


## Planner (WS3) — the second orchestration approach

`app/planner/` is a LangGraph flow for trip planning, deliberately **not** a port of the
concierge. The concierge stays on its custom async-generator orchestrator; full reasoning
in the root README, [Two orchestration approaches](../README.md#two-orchestration-approaches-and-why).

What makes this flow graph-shaped and the concierge's DAG not:

- a **replan cycle** — an empty or over-budget plan relaxes constraints and retries,
  bounded by `MAX_REPLANS` so an impossible budget cannot loop forever billing LLM calls
- a **human checkpoint** — `interrupt()` suspends the graph; `POST /api/planner/resume`
  continues it with approve or adjust
- a **checkpointer** — state outlives the request, which is the point: the free tier spins
  the instance down after 15 minutes idle and the trip has to survive that

`checkpointer.py` is a `BaseCheckpointSaver` over the existing Redis rather than
`langgraph-checkpoint-postgres`, which would pull psycopg alongside asyncpg for a handful
of small JSON blobs. Reads degrade to "no checkpoint" (starting over beats failing); a
failed **write** re-raises, because silently dropping it loses the turn on resume. 7-day
TTL so abandoned threads expire.

Verified by restarting the container between interrupt and resume — the thread resumed in
the fresh container with the plan intact.

Two things LangGraph does not give for free, both kept: per-node SSE step events on the
existing event vocabulary, and per-node error guards. `review` is the one deliberately
unguarded node — `interrupt()` raises a control-flow exception LangGraph must see, and
catching it would turn the human checkpoint into a silent approval.

## Reranking (WS4) — built, measured, disabled

`app/rerank.py`, lazy and flag-gated (`RERANK_ENABLED=false`). It is off because of the
RSS figure above, not preference: the smallest supported cross-encoder adds **+156 MB**
against 33 MB of headroom, and takes 20.8s to load. Reranking 50 documents takes ~1s on
one vCPU, which also rules it off the streaming path.

`scripts/rerank_eval.py` measures what it would buy without loading the model into the API
process: **top-10 overlap 3.7/10, top-1 changed in 5 of 6 queries**. A real effect — which
is what makes disabling it a trade rather than a dismissal.

`rerank_indices()` returns `None` ("no opinion") whenever the flag is off or the model
fails, so retrieval keeps its existing order rather than degrading.
