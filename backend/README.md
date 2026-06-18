# Backend — FastAPI

API service exposing **both** the traditional search/filter endpoints **and** the streaming multi-agent concierge.

> **Status:** implemented & verified (Phases 2 + 3). All endpoints below are live; the 4-agent concierge streams over SSE. Only the batch-compare **AI verdict** is deferred (the comparison matrix itself works).

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
    ├── retrieval.py     # semantic + filtered + geospatial search, ranked + rationale
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
| `POST` | `/api/batch/compare` | Compare 2–4 listings (parallel review synthesis for the AI verdict) |
| `POST` | `/api/concierge/stream` | Multi-agent concierge over **SSE**; streams intermediate steps + answer tokens |
| `POST` | `/api/nl-search` | Parse NL → structured filters for the search bar / chips |

Interactive docs at `/docs` when running.

## Run

Normally started by the root `docker compose up --build`. Standalone:

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000   # needs a .env at repo root (see ../.env.example)
```

## Agents (`app/agents/`)

| Agent | Role | Grounding |
|---|---|---|
| `intent` | NL → `StructuredQuery` via structured-JSON LLM call; resolves vague dates ("late June") to ISO ranges | omits fields it can't determine (no fabrication) |
| `retrieval` | embeds intent (fastembed) → Qdrant search **fused with hard-constraint payload filters** → hydrates rows from Postgres | per-result rationale built **deterministically** from real fields — the LLM can't invent attributes |
| `review_intel` | searches review vectors → LLM synthesis ("praise X, complain about Y") | **mandatory `[r#]` citations** to real review rows; abstains honestly when no reviews |
| `itinerary` | LLM decides segment structure only; property selection + costing is **deterministic** via the availability function | totals from real prices; budget-checked; ranked swap-out alternatives per stay |

`orchestrator.py` is a custom async generator: routes by intent, runs each agent in a guard that emits `status:"error"` and **degrades to traditional filtered search** rather than crashing the stream, and records per-step token/latency via `observability.py`.

## Notes / decisions

- **LLM over REST, not the deprecated `google-generativeai` SDK** — `llm.py` calls Gemini `generateContent` / `streamGenerateContent` via httpx, with structured-JSON (`responseMimeType`), retry-on-429/5xx + backoff, and a one-shot JSON repair pass. Provider switch (`gemini` | `anthropic`) behind one module.
- **Custom orchestrator, not LangGraph/CrewAI** — first-class SSE step streaming + exact token/latency accounting; lighter for 4 cooperating agents.
- **fastembed (ONNX), not sentence-transformers/torch** — fits Render's free 512 MB. Same 384-dim `bge-small-en-v1.5` at ingest + query, so vectors share one space.
- **SSE, not WebSocket** — works through Render/Vercel over HTTPS; needs a long-lived host (not serverless).
- **Async everywhere** — asyncpg pool, async Qdrant/Redis; CPU-bound embedding via `asyncio.to_thread`. Redis caching (search, retrievals, review syntheses) degrades gracefully if Redis is down.

## Trade-offs / simplifications

- **Beds-as-capacity** — no `max_guests` column; guest filtering uses `beds`.
- **Availability filter is post-DB-pagination** — search `total` reflects pre-availability counts (fine at current scale).
- **`app/availability.py` mirrors `ingestion/availability.py`** — same hash/params; keep them in sync.
- **batch-compare AI verdict deferred** — the matrix (price/amenities/rating/calendar) is implemented; only the LLM verdict string is pending.
- Qdrant `.search` emits a deprecation warning under client 1.12 (functional; `query_points` migration deferred).
