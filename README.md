# Travel Discovery AI

AI-native travel discovery & booking — a Booking.com/Airbnb-style product surface with a multi-agent concierge brain underneath.

> **Status:** scaffold. Modules are skeletons with `TODO` markers; wiring, config, and the deploy stack are in place.

## Stack (Path A — free tier)

| Layer | Choice | Why |
|---|---|---|
| Frontend | Next.js on **Vercel** | Free CDN + HTTPS, great DX |
| Backend | FastAPI on **Render** (Docker, long-lived) | SSE/WebSocket streaming needs a long-lived process (serverless would cut it) |
| Relational | **Postgres** (Neon free) | All 50K listings + 200K review texts |
| Vector | **Qdrant** (Qdrant Cloud free 1GB) | All 250K embeddings @ 384-dim; split from Postgres satisfies the brief's "justify the store split" |
| Cache | **Redis** (Upstash free) | Repeated retrievals + review syntheses |
| LLM | **Gemini Flash** (free tier), Claude Haiku fallback | ~$0 demo cost |
| Embeddings | **bge-small-en-v1.5** (384-dim) via fastembed/ONNX, local at ingest | $0, no torch, fits free tiers |

<!-- TODO: replace with 3–5 line agent-framework justification, LLM/vector-DB "why", and a Mermaid architecture diagram. -->

## One-command local run

```bash
cp .env.example .env        # fill in GEMINI_API_KEY (or ANTHROPIC_API_KEY)
docker compose up --build   # postgres + qdrant + redis + backend + frontend
```

- Frontend: http://localhost:3000
- Backend API + docs: http://localhost:8000/docs

### Load data

```bash
# Generate synthetic data + run the full ingestion pipeline (enrich + embed + index)
docker compose run --rm ingestion python ingest.py

# OR restore the pre-built dump + Qdrant snapshot (fast path).
```

See [ingestion/README.md](./ingestion/README.md) for the data layer details (store split, enrichments, deterministic calendar, scale notes).

## Repo layout

| Path | What | Docs |
|---|---|---|
| `backend/` | FastAPI: traditional search/filter API + streaming multi-agent concierge | [backend/README.md](./backend/README.md) |
| `frontend/` | Next.js booking-style product surface + conversational concierge | [frontend/README.md](./frontend/README.md) |
| `ingestion/` | Re-runnable data generation + ingestion pipeline (enrich, embed, index) | [ingestion/README.md](./ingestion/README.md) |
| `docker-compose.yml` | Full local stack | — |

## Deployment (Path A — free tier)

All services have free tiers. Stand up the data stores first, backend next, frontend last.

1. **Data stores** — create a **Neon** Postgres project, a **Qdrant Cloud** free cluster (1 GB), and an **Upstash** Redis database; copy each connection string. Restore the pre-built Postgres dump + Qdrant snapshot (don't re-ingest against a remote DB).
2. **Backend (Render)** — New → Web Service → connect the repo (builds `backend/Dockerfile`). Set env vars in the dashboard: `DATABASE_URL`, `QDRANT_URL` + `QDRANT_API_KEY`, `REDIS_URL`, `GEMINI_API_KEY` (+ `ANTHROPIC_API_KEY`). Never bake keys into the image. Add a cron ping to `/health` (~10 min) to defeat the free-tier spin-down.
3. **Frontend (Vercel)** — import the repo (`frontend/`), set `NEXT_PUBLIC_API_URL` to the Render URL.
4. **Wire + verify** — lock backend CORS to the Vercel origin; confirm SSE streams over HTTPS end-to-end (no mixed-content, no proxy buffering).

<!-- TODO (deliverables, fill in-README before submission):
     - Mermaid architecture diagram
     - data choice (synthetic/real/mixed) + why
     - agent-framework + LLM + vector-DB choices with 3–5 line "why"
     - key trade-offs (e.g. deterministic calendar, 384-dim embeddings, demo-subset summaries)
     - what I'd change with another week (→ single-VM deploy: Oracle Always-Free / Hetzner)
     - rough cost per user query at production scale
     - hours actually spent -->

## Evaluation

See [EVAL.md](./EVAL.md) for the golden-query set, scoring rubric, and grounding/citation checks.
