# JD mapping — where each requirement actually lives

One row per JD section, each pointing at a real file you can open. Anything not
built yet says so; **no row points at a file that does not exist**, and nothing here
is aspirational. Status is as of 2026-08-11 on the `v2-agentic` branch.

> **A note on the grouping.** The job description is not in this repo, so the four
> sections below are reconstructed from the capability list recorded in
> `version2/CLAUDE.md` ("LangGraph/CrewAI/AutoGen, RAG with reranking, document OCR,
> MCP-style integrations, model benchmarking, and CI/CD"), anchored by the two
> explicit references in the plan docs: MCP is section 1 (`WS2_MCP.md`) and CI/CD is
> section 4 (`V2_PLAN.md`). If the real section numbering differs, only the numbers
> move — the file paths are what matter.

---

## 1 · Agentic systems, orchestration frameworks, MCP integrations

| Requirement | Where | Status |
|---|---|---|
| Multi-agent orchestration | `backend/app/agents/orchestrator.py` — custom async-generator over intent / retrieval / review-intel / itinerary, with per-step SSE events and token accounting | ✅ shipped in v1 |
| Composite routing | `orchestrator.py::_classify` — returns an ordered `list[str]`; a query asking for stays *and* review synthesis runs both pipelines and merges contexts before the answer | ✅ WS0-F |
| **MCP server** (platform as a tool) | `backend/app/mcp_server/server.py` — six tools mounted at `/mcp` inside the existing FastAPI app, sharing the asyncpg pool, Qdrant client and Redis | ✅ WS2 |
| **MCP client** (platform consuming a tool) | `backend/app/weather.py` — consumed by `agents/itinerary.py::plan_itinerary`, one call per plan, feeding `plan["notes"]` | ✅ WS2 — **local only by decision**, not deployed (free-tier instance-hours; see `WS2_MCP.md`) |
| MCP auth + rate limiting | `backend/app/mcp_server/auth.py` — bearer auth as ASGI middleware, failing **closed** when unset; RPM cap on the two LLM-backed tools only | ✅ WS2 |
| Agent memory | `backend/app/memory/store.py` — traveller + trip scopes via mem0; dealbreakers become hard Qdrant payload filters, not prompt hints | ✅ WS1 |
| **LangGraph planner** (cycles, HITL interrupt/resume, checkpointer) | `backend/app/planner/graph.py` — replan cycle bounded at `MAX_REPLANS`, `interrupt()` human checkpoint, conditional routing; `checkpointer.py` — `BaseCheckpointSaver` over the existing Redis. `routers/planner.py` exposes `/api/planner/stream` and `/resume` | ✅ WS3 — interrupt/resume **verified across a container restart** |

**Why both, rather than one framework everywhere:** the concierge is a short, mostly
linear route where what mattered was SSE step streaming and exact per-step token
accounting — a few lines in a generator, awkward to retrofit onto a framework's callback
model. The planner has a replan cycle, a human checkpoint and state that must outlive the
request, which is genuinely graph-shaped. The concierge was **not** ported. Full reasoning
in the root README, [Two orchestration approaches](../README.md#two-orchestration-approaches-and-why).

## 2 · RAG with reranking

| Requirement | Where | Status |
|---|---|---|
| Vector retrieval | `backend/app/agents/retrieval.py` — Qdrant `listings` (50K) + `summaries` (50K), both 384-dim, same fastembed model at ingest and query so vectors share one space | ✅ v1 |
| Hybrid / fusion | `retrieval.py` — the two collections are fused by **Reciprocal Rank Fusion (k=60)**, chosen over max-score because it is scale-free across two differently-distributed score sets | ✅ WS0-H |
| Hard-constraint filtering | `retrieval.py::_build_qdrant_filter` — payload conditions on the six indexed fields, plus WS1 dealbreakers as `must`/`must_not` | ✅ |
| Grounding | Rationales are built deterministically from real Postgres fields, so a rationale cannot claim an attribute the listing lacks | ✅ v1 |
| Review grounding | `backend/app/agents/review_intel.py` — Postgres full-text over 200K real reviews, aspect-polarity sampling, mandatory `[r#]` citations to real `reviews.id` | ✅ WS0-B |
| **Cross-encoder reranking** (retrieve 50 → rerank 10) | `backend/app/rerank.py` (lazy, flag-gated), `agents/retrieval.py::_apply_rerank`, `scripts/rerank_eval.py` (offline delta measurement) | ✅ WS4 — built and measured, **disabled on the free tier**: +156 MB against 33 MB of headroom. Measured effect: top-10 overlap 3.7/10, top-1 changed 5/6 |

Honest caveat: reviews are **not** vector-embedded — embedding 200K long reviews on
a 4-core CPU was ~15 hours. Review search is Postgres full-text; the per-property
summary vectors and the LLM reading real rows cover the semantic gap. Recorded as
trade-off #1 in the root README rather than glossed.

## 3 · Document OCR / structured extraction

| Requirement | Where | Status |
|---|---|---|
| Booking-confirmation OCR → structured fields → trip memory | — | ❌ **not built** (WS6) |

The nearest shipped relative is `backend/app/agents/intent.py`, which does
structured extraction from natural language via a JSON-schema LLM call with
defensive post-validation against a closed vocabulary — the same
extract-then-validate shape WS6 would use, minus the OCR front end.

## 4 · Model benchmarking and CI/CD

| Requirement | Where | Status |
|---|---|---|
| CI pipeline | `.github/workflows/ci.yml` — ruff + pytest + docker build, pinned actions, Python 3.11 | ✅ **green on `v2-agentic`** (run 31482009729). Note ~23 MCP tests do NOT run there: `test_mcp_server.py` importorskips `fastmcp`, which is deliberately absent from `requirements-dev.txt` |
| Tests | 273 backend tests locally (249 in CI, where the MCP suite skips); `backend/tests/conftest.py` stubs the heavy deps and blocks network so the suite consumes **zero LLM quota** | ✅ WS7 |
| E2E | `frontend/e2e/` — 12 Playwright tests against the real restored corpus | ✅ |
| Provider/model switching | `backend/app/llm.py` — Gemini and Anthropic behind one module, with `model`/`provider` overrides on all five public functions so a benchmark can switch without restarting the process | ✅ WS0-D |
| Measured token usage | `llm.py::stream_text_with_usage` + `orchestrator.py` — real `usageMetadata`, tagged `usage_source: measured` vs `estimated`, replacing v1's per-chunk proxy | ✅ WS0-D |
| Quality eval | `EVAL.md` — golden-query set with manual scoring | ⚠️ **stale**: scored on a different model and before WS0-B/E/H, so it needs re-running, not extending |
| **Automated benchmark harness** (models × golden queries → cost/latency/accuracy) | `scripts/benchmark.py` — intent field-level F1, citation validity against real `reviews.id`, answer entity containment. No LLM judge. | ✅ WS5 — results and recommendation in `EVAL.md` |

---

## Summary

Sections **1**, **2** and **4** are delivered; **3** is not started.

Section 1 is now complete end to end: multi-agent orchestration, composite routing, an MCP
server AND client, agent memory, and a LangGraph flow with cycles and a human checkpoint.

The only remaining gap is WS6 (OCR), the plan's own designated cut. That is a scheduling fact rather than a technical
blocker: WS5 needs the service layer (`backend/app/services/`) and a trustworthy EVAL
baseline, both of which now exist.
