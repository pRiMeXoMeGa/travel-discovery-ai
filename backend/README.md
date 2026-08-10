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
│   └── agents.py      # POST /api/concierge/stream (SSE), POST /api/nl-search
└── agents/
    ├── orchestrator.py  # coordinates the 4 agents, yields step events for streaming
    ├── intent.py        # NL -> StructuredQuery
    ├── retrieval.py     # semantic (listings + summary vectors, RRF-fused) + hard-filtered
    │                    # + neighbourhood-level area constraints; ranked + grounded rationale
    ├── review_intel.py  # grounded review synthesis with citations
    └── itinerary.py     # multi-day, multi-property plans
```

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/health` | Liveness (also the keep-warm ping target) |
| `POST` | `/api/search` | Filtered/sorted search with calendar availability |
| `GET`  | `/api/listings/{id}` | Property detail (gallery, amenities, aspect scores, summary, price breakdown) |
| `GET`  | `/api/listings/{id}/reviews` | Reviews filtered by language / score / topic |
| `POST` | `/api/batch/compare` | Compare 2-4 listings (parallel review synthesis for the AI verdict) |
| `POST` | `/api/concierge/stream` | Multi-agent concierge over SSE; streams intermediate steps + answer tokens |
| `POST` | `/api/nl-search` | Parse NL into structured filters for the search bar / chips |

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
