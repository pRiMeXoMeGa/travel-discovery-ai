# Backend — FastAPI

API service exposing **both** the traditional search/filter endpoints **and** the streaming multi-agent concierge.

> **Status:** scaffold. Wiring, config, routing, and clients are in place; endpoint/agent bodies are `NotImplementedError` / `TODO`.

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

## Notes / decisions

- **fastembed (ONNX), not sentence-transformers/torch** — small enough to fit Render's free 512 MB. Same 384-dim `bge-small-en-v1.5` model used at ingest, so query and corpus vectors share one space.
- **SSE, not WebSocket** — simpler and works through Render/Vercel over HTTPS; requires a long-lived host (not serverless).
- **Single uvicorn worker** in the Dockerfile — keeps memory low and avoids duplicate embedding-model loads on free tier.
- **Async everywhere** — asyncpg pool, async Qdrant/Redis clients; CPU-bound embedding runs via `asyncio.to_thread`.
