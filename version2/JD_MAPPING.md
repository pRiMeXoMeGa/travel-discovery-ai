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
| **MCP client** (platform consuming a tool) | `backend/app/weather.py` — consumed by `agents/itinerary.py::plan_itinerary`, one call per plan, feeding `plan["notes"]` | ✅ WS2 |
| MCP auth + rate limiting | `backend/app/mcp_server/auth.py` — bearer auth as ASGI middleware, failing **closed** when unset; RPM cap on the two LLM-backed tools only | ✅ WS2 |
| Agent memory | `backend/app/memory/store.py` — traveller + trip scopes via mem0; dealbreakers become hard Qdrant payload filters, not prompt hints | ✅ WS1 |
| LangGraph planner (cycles, HITL interrupt/resume, checkpointer) | — | ❌ **not built** (WS3) |

**Why the custom orchestrator and not a framework:** v1 needed first-class SSE step
streaming and exact per-step token/latency accounting for four cooperating agents,
which is lighter to hand-roll than to retrofit. LangGraph is planned for the *new*
trip-planner flow specifically because that flow is genuinely graph-shaped
(budget-exceeded replan cycle, no-availability alternatives cycle, HITL interrupt
before commit) — not to port work that already functions.

## 2 · RAG with reranking

| Requirement | Where | Status |
|---|---|---|
| Vector retrieval | `backend/app/agents/retrieval.py` — Qdrant `listings` (50K) + `summaries` (50K), both 384-dim, same fastembed model at ingest and query so vectors share one space | ✅ v1 |
| Hybrid / fusion | `retrieval.py` — the two collections are fused by **Reciprocal Rank Fusion (k=60)**, chosen over max-score because it is scale-free across two differently-distributed score sets | ✅ WS0-H |
| Hard-constraint filtering | `retrieval.py::_build_qdrant_filter` — payload conditions on the six indexed fields, plus WS1 dealbreakers as `must`/`must_not` | ✅ |
| Grounding | Rationales are built deterministically from real Postgres fields, so a rationale cannot claim an attribute the listing lacks | ✅ v1 |
| Review grounding | `backend/app/agents/review_intel.py` — Postgres full-text over 200K real reviews, aspect-polarity sampling, mandatory `[r#]` citations to real `reviews.id` | ✅ WS0-B |
| **Cross-encoder reranking** (retrieve 50 → rerank 10) | — | ❌ **not built** (WS4) |

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
| CI pipeline | `.github/workflows/ci.yml` — ruff + pytest + docker build, pinned actions, Python 3.11 | ⚠️ **written, never executed** — nothing has been pushed |
| Tests | 273 backend tests; `backend/tests/conftest.py` stubs the heavy deps and blocks network so the suite consumes **zero LLM quota** | ✅ WS7 |
| E2E | `frontend/e2e/` — 12 Playwright tests against the real restored corpus | ✅ |
| Provider/model switching | `backend/app/llm.py` — Gemini and Anthropic behind one module, with `model`/`provider` overrides on all five public functions so a benchmark can switch without restarting the process | ✅ WS0-D |
| Measured token usage | `llm.py::stream_text_with_usage` + `orchestrator.py` — real `usageMetadata`, tagged `usage_source: measured` vs `estimated`, replacing v1's per-chunk proxy | ✅ WS0-D |
| Quality eval | `EVAL.md` — golden-query set with manual scoring | ⚠️ **stale**: scored on a different model and before WS0-B/E/H, so it needs re-running, not extending |
| **Automated benchmark harness** (models × golden queries → cost/latency/accuracy) | — | ❌ **not built** (WS5) |

---

## Summary

Sections **1** and **2** are substantially delivered; **4** is delivered except the
automated benchmark and an actual CI run; **3** is not started.

Three of the four gaps are the same three unstarted workstreams (WS3, WS4, WS5, WS6),
which is a scheduling fact rather than a technical blocker — the enabling work each
depends on is done: WS3 and WS5 both needed the service layer (`backend/app/services/`),
and WS4 needs the fused retrieval path it now has.
