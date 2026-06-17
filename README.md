# Travel Discovery AI

AI-native travel discovery & booking — a Booking.com/Airbnb-style product surface with a multi-agent concierge brain underneath. Built against the [assignment brief](./ASSIGNMENT.md); see [plan.md](./plan.md) for the full build plan and architectural decisions.

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

# OR restore the pre-built dump + Qdrant snapshot (fast path) — see ingestion/README.
```

## Repo layout

```
backend/     FastAPI: traditional search/filter API + streaming multi-agent concierge
frontend/    Next.js booking-style product surface + conversational concierge
ingestion/   Re-runnable data generation + ingestion pipeline (enrich, embed, index)
docker-compose.yml   Full local stack
```

## Deployment (Path A)

See [plan.md → Phase 6](./plan.md) for the step-by-step runbook (Neon + Qdrant Cloud + Upstash + Render + Vercel).

<!-- TODO (deliverables): data choice + why · key trade-offs · what I'd change with another week
     (→ single-VM Path B) · rough cost per query at production scale · hours actually spent. -->
