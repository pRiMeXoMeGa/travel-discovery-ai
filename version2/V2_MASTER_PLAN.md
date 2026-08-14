# Travel Discovery AI v2 — Master Plan

Consolidates `V2_PLAN.md`, `WS1_MEMORY_INTEGRATION.md`, `WS2_MCP.md` and the v1 audit in
`FINDINGS.md`, then **revised against two independent verification passes** — one
infrastructure/deployment, one agent/LLM design. Claims marked ✔ were re-verified directly
against the repo or package source, not taken on trust.

**Thesis:** v1 answered travel questions. v2 makes the platform agentic — it remembers the
traveller, calls out to external tools, and exposes itself as a tool other agents use.

---

## 0. What verification changed

Five findings materially rewrote this plan. All confirmed against the code.

| # | Finding | Effect |
|---|---|---|
| V1 | **The Qdrant `summaries` collection is never read.** ✔ Every `summaries` hit in `backend/app/` is the Postgres `listing_summaries` *table*. `review_intel.py:26,29` imports `embed_query` and `get_qdrant` and never calls them | Kills the WS0-A → WS4 dependency as originally written; adds **WS0-H**; answers EVAL failure #3 definitively; makes a v1 README claim false |
| V2 | **Write-time dealbreaker polarity is unimplementable through mem0.** `add(infer=True)` takes one flat metadata dict for the whole extracted batch, and returns memories after the sentence is gone | Dealbreaker extraction **moves into the intent call**, which already runs, already sees the raw sentence, and already returns validated structured output. Zero extra calls |
| V3 | **The trip-scope mirror corrupts trip semantics.** CLAUDE.md defines trip scope as cities/dates/session constraints/bookings — disjoint from traveller facts. Mirroring fills it with duplicates and drops the per-fact metadata | Trip scope becomes **derived structured state** from the already-parsed `StructuredQuery`, written `infer=False`. Zero LLM calls, deterministic |
| V4 | **mem0's LLM calls bypass `app/llm.py` entirely** — its Gemini path uses `google-genai`, its own client, its own retry | Memory cost is invisible to `RequestTrace`, to WS0-D, to WS5's cost table, and to `llm.py`'s 429 backoff. Must be instrumented separately |
| V5 | **RSS worst case is 550–700 MB against a 512 MB limit** — bge-small ONNX 250–300 MB + baseline 100–150 MB + mem0 stack 50–100 MB + cross-encoder 150–200 MB | Memory becomes a **gate before deploy**, not a risk to review after. Cross-encoder is the pre-committed cut |

Also resolved: `TextCrossEncoder` **does** exist in `fastembed==0.4.2` ✔ (WS4's pin question
is closed). mem0 does **not** pull torch in a base install ✔. Qdrant 1 GB headroom is
fine ✔. FastMCP lifespan chaining is sound ✔ — `fastmcp/server/http.py` raises exactly the
error the spec anticipates.

---

## 1. The core planning insight

The workstreams are not independent. Each sits on a v1 defect, and shipping the workstream
without fixing the defect produces a feature that demos badly or makes a false claim.

| v2 workstream | Blocked by | Sev |
|---|---|---|
| WS4 reranking | **V1** — summary vectors unread; and `listings` embedding text is a template (`ingest.py:1136-1145`) whose every field *except `name`* is already a hard filter, so a reranker has almost nothing to discriminate on | HIGH |
| WS2 `synthesize_reviews` | 2.2 — sampler orders by an always-NULL column | HIGH |
| WS5 benchmark | Answer-step tokens are a `+= 1` proxy (`orchestrator.py:141`); **and** mem0's calls are invisible (V4) | HIGH |
| WS2 tools generally | Layering: `orchestrator.py:63`, `routers/agents.py:12-14,57` | HIGH |
| WS1 dealbreakers, WS3 planner | 2.3 — no geospatial retrieval; `near_areas` computed and never read | HIGH |
| WS3 planner | 2.4 — single-route `_classify` | HIGH |

**WS0 comes first.** It is the prerequisite set, and it doubles as the v1 roadmap delivery
that gives v2 its framing.

---

## 2. Framing (honest mapping to v1's README promises)

| v1 "another week" bullet | v2 |
|---|---|
| Embed all 200K reviews for per-review semantic search | **Not delivered.** WS0-H wires the *existing* summary vectors — a different, smaller thing. Say so |
| LLM aspect sentiment + per-property summaries | **Half delivered** — WS0-A does the summaries (max-evidence subset, `provenance` flag, honest UI label for the rest). Aspect sentiment stays heuristic **on purpose**: it is fed to the answer model as "AGGREGATE ASPECT SCORES", so a model-estimated value would be presented as measured. Do not claim this one as done |
| Single always-on VM | Not planned |
| Materialize calendar / `.search` → `query_points` | **Partially** — new code only |

WS5 and WS6 are JD-driven, not roadmap delivery. Justify on product merit.

---

## 3. Constraints

1. No torch, no sentence-transformers. **512 MB is a gate, not a guideline** (V5).
2. 384-dim local fastembed, same model at ingest and query.
3. Gemini via REST through `app/llm.py`; Anthropic fallback; **no OpenAI**. Note mem0 and
   LangGraph both default toward OpenAI — assert at startup and grep after every step.
4. SSE step streaming is the contract.
5. Graceful degradation per component; the stream never crashes.
6. Async everywhere. **One vCPU** — `to_thread` moves work off the event loop, not off the
   core.
7. Docs match v1's bar.
8. Render free = **750 instance-hours/month per account** → one near-always-on service.
9. Qdrant Cloud free = 1 GB (~300–400 MB used ✔).

---

## 4. Dependency graph

```
WS0-C service layer ──┬─→ WS2 MCP ──→ Claude Desktop screenshot
                      ├─→ WS3 LangGraph
                      └─→ WS5 benchmark
WS0-H summaries wired ─┬─→ WS0-A LLM summaries (only now does it affect retrieval)
                       └─→ WS4 reranking (only now does it have signal)
WS0-B sampler ─────────→ WS2 synthesize_reviews
WS0-D token accounting ┬→ WS5
                       └→ WS1 (mem0 usage must be captured too — V4)
WS0-E geo ─────────────┬→ WS1 dealbreakers · WS3 spatial constraints
WS0-F routing ─────────→ WS3   (fix in WS0 — WS3 never touches _classify)
WS1 memory ────────────→ WS6 OCR
WS7 CI — first, so it guards everything after
```

**Critical path:** WS0-C → WS2 → WS3.

---

## 5. WS0 — v1 debt paydown (~2.5 days)

### WS0-B · Review sampler — 1h · unblocks WS2
Ordering by `rating` is a no-op (always NULL). **But do not simply swap in `sentiment`** —
it is NULL for non-English reviews, so `NULLS LAST` would silently make the evidence set
English-only, contradicting `review_intel.py:45-47` which tells the model to read all
languages. It is also heavily tied (`(pos-neg)/total` over ≤5 aspects) and, at ~4
reviews/listing, both halves return everything anyway.

**Do instead:** sample by *aspect polarity presence* — top-4 rows with any aspect score
> 0, top-4 with any < 0, language diversity as tiebreak, `, id` appended to both `ORDER BY`s
for determinism. Degrades gracefully on thin listings and keeps the multilingual corpus.
Also fix `_fallback_summary` thresholds (`>= 4.0`/`<= 3.0` are rating-scale; sentiment is
`[-1,1]`) and surface `sentiment` in the snippet dict and evidence line where the
always-`n/a` rating currently sits.

### WS0-D · Token accounting — 2h · unblocks WS5, WS1
Parse `usageMetadata` from the final `streamGenerateContent` frame. Also: `_anthropic_stream`
(`llm.py:320-349`) has **no retry loop and reports no usage** unlike `_gemini_stream` — fix
the asymmetry or the Haiku benchmark column is not comparable. And add a `model`/`provider`
override parameter to `llm.py`'s five public functions, or WS5 can only benchmark by
restarting the process (V4/F10).

### WS0-C · Service-layer extraction — 1.5h · unblocks WS2, WS3, WS5
`app/services/{listings,reviews,availability,planning}.py`. Removes `orchestrator.py:63`'s
router-private import and `routers/agents.py`'s sibling-router + `_parse_constraints`
imports. Tools never call our own HTTP endpoints.

### WS0-H · Wire the summaries retrieval path — 2h · unblocks WS0-A and WS4 **[NEW]**
The `summaries` collection is built, published, restored — and never queried (V1). Add
`qdrant_collection_summaries` to `Settings`, search it with the same query vector, fuse
into the listings hits by `listing_id` (RRF or max-score). Without this, WS0-A is a
UI/MCP-credibility change only and WS4 has nothing to rerank.

Also remove the now-false README line claiming summary vectors soften the recall trade-off,
or make it true by shipping this.

### WS0-E · Geospatial retrieval — 3h · unblocks WS1, WS3
`near_areas` is computed and never read; there is no geo condition at all. Add a per-city
alias table (place name → neighbourhood values) fed as Qdrant conditions on the
**already-indexed** `neighbourhood` keyword field — no new payload index, no re-snapshot.
Defer lat/lng radius (needs a `geo` index + re-snapshot).

### WS0-F · Routing — 3h · **fix here, not in WS3**
WS3 by its own charter does not touch the concierge, and `_classify` lives in
`orchestrator.py:41`. Deferring means it is never fixed.

Broader than "nights beats review": `_PLANNING_KEYWORDS` contains `days`/`plan`/`route`, so
"2 nights in Lisbon under 130" → itinerary; `_REVIEW_KEYWORDS` contains `clean`, so "a clean
apartment" → review. **Require a structural signal for `itinerary`** (≥2 stay-intent markers,
or `(check_out - check_in).days >= 3` *and* a multi-stay phrase) — finally a real use for
the unused `sq` parameter. Keep substring matching on the review side only.

Two blockers the original plan missed:
- `_emit_route_events` (`orchestrator.py:248-253`) iterates **all** of `trace.steps` from
  index 0 every call. Two route runners ⇒ the first runner's steps are re-emitted, and the
  frontend dedupes by agent name so they silently overwrite. Take a start offset.
- `route` is a bare string on the wire and feeds `_answer_prompt`. Keep `route: str`, add
  `routes: list[str]`, and reuse `_run_search`'s candidate set for `_run_review` rather than
  re-retrieving at `limit=3`.

### WS0-A · LLM per-property summaries — 4h — ✅ DONE 2026-08-12
`scripts/backfill_summaries.py` (standalone, resumable, `--dry-run`), offline, re-embeds.

> **N is smaller than planned, for a measured reason.** The plan assumed top-N by
> `review_count` over 2–5K listings. The corpus caps reviews at **10 per listing**
> (`max(count(*)) = 10`; 1,286 listings sit at the cap), so the ranking saturates and any N
> beyond that is chosen by UUID tiebreak — i.e. arbitrary. The run targets the 1,286
> max-evidence listings instead. Note also that `listings.review_count` disagrees with
> reality (a row reporting 1909 has 3 review rows, FINDINGS §6.7), so the script orders by
> `count(*)` on `reviews`.
>
> **Point ids are integers.** `ingest.py:1204` keys `summaries` by `UUID(lid).int >> 64`.
> Upserting under the listing_id string does not error — it duplicates every point and
> leaves the stale vector matching. Verified 50,000 before and after.
- **Keep the heuristic `aspect_avg`.** `summarize_property(use_llm=True)` overwrites it with
  model-*estimated* scores, and `aspect_avg` is fed to the review agent as "AGGREGATE ASPECT
  SCORES" (`review_intel.py:138-147,198`) — a grounding regression in the most
  grounding-sensitive agent. Take only `summary` from the LLM.
- Add a `provenance` column ('llm'|'heuristic') to `listing_summaries` — the plan says
  "badge it in the UI" but currently stores nothing to badge with.
- The LLM summary is uncited generated prose under a sparkle icon. **Resolved by labelling:**
  `summary_provenance` reaches the client, the sparkle panel renders only for `'llm'`, and
  heuristic rows now say *"What guests said · quoted from reviews"*. That matters more than
  the backfill itself — a subset backfill shrinks the mislabelled set, it does not fix it,
  and ~48,700 listings will keep their extractive summaries.

### WS0-G · Repro + drift — 1.5h
compose `csvData` volume mount · model name aligned to `gemini-3.1-flash-lite` in
`config.py:32` and `EVAL.md` · **unify the amenity vocabulary** — `retrieval._KNOWN_AMENITIES`
carries `dryer`/`heating`/`tv` (absent from the payload, never match) and omits `baby_cot`
(silently dropped) ✔; import one constant · four hardcoded `$` sites · drop the false
geospatial/topic-filter doc claims.

---

## 6. WS1 — mem0 memory (~1.5 days, revised)

**Redesigned in three places by verification.**

### 6.1 Dealbreaker capture moves into the intent call (V2)
Not into mem0's inferred `add()`, which cannot attach per-fact metadata and has lost the
sentence by the time it returns. Extend `_INTENT_SCHEMA`:

```
"dealbreakers": "array of {field:'amenities'|'type', value:<exact vocab term>, op:'must'|'must_not'} —
                 only for absolute never/always statements"
"suppress_dealbreakers": "array of {field, value} the CURRENT turn overrides"
```

Enumerate the 18 amenities and 4 room types in `_SYSTEM`. Validate through
`store.validate_dealbreaker()`, persist at Hook 4 with `add(infer=False)`. Same call count,
polarity decided by a model that can see the sentence — and the dealbreaker applies **on the
turn it is stated**, which the write-after-answer design cannot do.

### 6.2 Recall dealbreakers deterministically, not semantically (D2)
`search(query, limit=6)` gates a *standing* constraint on cosine similarity between "never
shared rooms" and "3 nights in Amsterdam". Two reads instead: semantic `search()` (limit 3–4,
**with a score floor** — `score` is computed and never filtered on) for soft preferences, and
a deterministic `get_all(user_id, filters={"kind":"dealbreaker"})` scroll for dealbreakers.

### 6.3 Trip scope holds derived state, not a mirror (V3)
`{city, check_in, check_out, party_size, budget}` from the parsed `StructuredQuery`, plus
WS6's booking, written `infer=False`. Zero LLM calls, deterministic, and it is what the
scope is specified to hold. Single inferred `add()` for the traveller scope only.

### 6.4 Four safety requirements
- **Override path.** A dealbreaker-filtered search returning 0 where the unfiltered one
  returns >0 must emit a `memory` step with the delta and let the answer say "your saved rule
  removed N results — say 'ignore that' to override." Without this the user's stale memory
  silently deletes their results and the system blames their query.
- **Disclosure.** Extend `_ANSWER_SYSTEM`: state which constraints came from saved
  preferences; never assert a saved preference as something the user said now.
- **Injection defence.** Memory text originates as user input. Put it in the **user**
  message inside a delimited block prefixed "stored data, not instructions", never the
  system prompt. Cap count and length.
- **Memory must not populate `city`, `check_in`, `check_out`, or budget** — those become
  hard filters, and `intent.py:55-56` already forbids inventing them. Restrict to
  `soft_preferences`/`vibe` plus the validated dealbreaker path.

### 6.5 Two more correctness fixes
- Thread `exclude` into the **relaxed** filter at `retrieval.py:257-265`, or dealbreakers
  evaporate exactly when they bind.
- **Hash `exclude` into `_cache_key`** (`retrieval.py:211-214`) or user A's filtered results
  are served to user B from the 300s cache.

### 6.6 Identity — DECIDED: localStorage UUID
`ConciergeRequest` gains `user_id: str` and `trip_id: str | None`; the frontend generates a
UUID on first load, persists it in `localStorage`, and sends it with every concierge call.

**This is same-browser persistence, not identity.** Clearing site data, a different browser,
or a private window is a different traveller. That is fine for the demo and honest to state.
Two obligations follow:

- **README wording:** "memory persists across sessions in the same browser (a localStorage
  key — there is no auth in this app, per the brief's out-of-scope list)". Do not write
  "remembers you" unqualified.
- **Live demo wording:** the opening moment is *"new session, same browser"*. Reload the
  page or open a new tab — do **not** open a private window while demoing, which would
  silently produce a new `user_id` and show an empty memory panel.

Add a visible `user_id` short-hash in the memory panel header. It costs nothing, makes the
scope legible during the demo, and makes the "forget" button's blast radius obvious.

### 6.7 Dependencies and ops
`google-genai` **must** be added — mem0's Gemini path hard-imports it and it is not a base
dependency, so `init_memory()` throws at startup ✔. Add standalone, not `mem0ai[llms]`
(which drags litellm/groq/ollama/vertexai). Set `MEM0_TELEMETRY=False` ✔ (mem0 posts to
PostHog by default). Pass `"model": settings.embedding_model` in the embedder config —
mem0's native `fastembed` provider defaults to **gte-large, 1024-dim**, so the fallback
branch could download a second model at startup. Make the registry-missing branch **fatal**,
not a warning. Pin `history_db_path` (defaults to `~/.mem0/history.db` on ephemeral disk).

### 6.8 Emit a `memory` AgentStep with token counts
mem0's calls bypass `llm.py` (V4), so without this the largest new cost per turn is absent
from `RequestTrace` and from WS5's table.

---

## 7. WS2 — MCP, both directions (~1 day)

Unchanged in substance; verification confirmed the design. Part 0 = WS0-C.

Six tools mounted at `/mcp` inside the existing app (lifespan chained ✔). Four are not pure
wraps: `compare_listings` currently always spends up to 5 LLM calls; `synthesize_reviews`
has no abstention flag; `check_availability` needs a range→days helper; `min_rating` is
unenforceable on the semantic path. `City` must be title case. Bearer auth via ASGI
middleware with `hmac.compare_digest` ✔.

**Pin `fastmcp==2.14.*`** — PyPI now serves a 3.x line, so an unpinned requirement resolves
to 3.4.6 and the verified `@mcp.tool` / `http_app()` surface is 2.x ✔.

Inbound weather: one keep-warm service only. Let weather cold-start and degrade on the 3s
timeout — and note v1's keep-warm ping **is not actually configured yet** (`FINDINGS.md` §1),
so set that up first or an interviewer's first message hits two cold services at once.

---

## 8. WS3 — LangGraph planner (~1.5 days, revised from 1)

Do not port the concierge. Add the graph for the planner only: budget/availability cycles,
preference-conflict clarification, HITL interrupt, checkpointer.

**DECIDED: full interrupt/resume contract.** Budget it properly — this is the single
largest new surface in v2.

**1. The SSE contract changes on both ends.** `run_concierge` is single-shot: it yields to
`done` (`orchestrator.py:158`) and holds no cross-request state; the frontend treats `done`
as terminal (`ConciergePanel.tsx:390-392`). Full HITL needs:

- a new terminal event `{"type": "awaiting_input", "thread_id": str, "interrupt": {...}}`
  emitted **instead of** `done` when the graph interrupts;
- `POST /api/planner/resume` taking `{thread_id, decision}`, returning a fresh SSE stream
  that continues the same graph run;
- `ConciergeRequest` (or a sibling `PlannerRequest`) carrying `thread_id`;
- frontend: hold the turn open on `awaiting_input`, render approve/adjust controls, and
  re-enter `streamConcierge` against the resume endpoint. `ConciergePanel.send()` currently
  assumes one request per turn — this is a real refactor, not a branch.

**2. Checkpointer — use Redis via the existing client, not Postgres.**
`langgraph-checkpoint-postgres` pulls **psycopg** alongside the existing asyncpg: a second
Postgres driver on a box that (with WS4 kept) is already near the ceiling. In-memory is
worse — a free-tier cold start between interrupt and resume loses the thread, which is
precisely the window a human takes to decide.

Implement a small `BaseCheckpointSaver` over the **Upstash Redis connection that already
exists** (`app/cache.py`). It is a narrow interface (`aget_tuple` / `aput` / `alist`),
roughly ~100 lines, adds **zero new drivers and zero new RAM**, survives cold starts because
Upstash is external, and TTL semantics match HITL threads, which are short-lived by nature.
Set a 24h TTL and document that an abandoned plan expires.

This is the decision that lets WS4 and full HITL coexist inside 512 MB.

**3. Graph nodes call `app/llm.py`, not a LangChain LLM wrapper** — otherwise WS0-D covers
half the pipeline, retry/backoff splits in two, and the OpenAI default sneaks in.

**4. Keep `RequestTrace` outside graph state** — a trace inside checkpointed state is
replayed on resume and double-counts tokens. Append to it from streamed custom events.

Keep `RequestTrace` **outside** graph state; append to it from streamed custom events. A
trace inside checkpointed state is replayed on resume and double-counts tokens.

---

## 9. WS4 — Cross-encoder reranking (~4h) — **DECIDED: keep**

`TextCrossEncoder` confirmed present in `fastembed==0.4.2` ✔, default
`Xenova/ms-marco-MiniLM-L-6-v2`, 0.08 GB on disk / ~150–200 MB loaded.

Keeping it is a deliberate choice against a real memory ceiling, so it comes with three
engineering requirements rather than a hope:

**a. WS0-H is a hard prerequisite, not a preference.** Without a summaries path, reranking
reorders `listings` vectors whose embedding text is a template
(`ingest.py:1136-1145`) where every field except `name` is already a hard payload filter.
The honest prior on the delta is zero-to-negative — you would ship a measured null result.
If WS0-H slips, WS4 slips with it.

**b. Lazy-load and keep it off the streaming path.** Do not construct `TextCrossEncoder` at
import or in the lifespan — load on first rerank so a cold start that never reranks never
pays the RSS. Apply reranking to `/api/search` and the MCP `search_listings` tool; do **not**
put it inside the concierge's streaming turn, where it would contend with token generation
for the single vCPU (R2b).

**c. Rerank 50 → 10 with a bounded input.** Cross-encoder cost is per pair; 50 pairs of
short template text is the budget. Do not raise the candidate count "to see if it helps".

**RSS gate still applies** — measure with `docker stats` after WS1 and again after WS4. If
the box is over, the mitigation is now (b) taken further (rerank behind a feature flag,
default off in production, on for the measured EVAL run), **not** dropping the workstream.

---

## 10. WS5 — Benchmark (~4h, method redesigned)

Manual 1–5 scoring × 3 models × 10–15 queries will not happen in 3h and is not repeatable.
Swapping `GEMINI_MODEL` also changes intent parsing, segment structure and answer fidelity
at once, so a single "accuracy" score confounds them.

**Per-stage, fixed-input, automatable — no LLM judge:**
1. *Intent*: golden query → expected `StructuredQuery`, field-level exact match / F1.
2. *Review synthesis*: fixed snippet set; assert every sentence carries `[r#]`, every `[r#]`
   resolves in `label_to_id`, no numeral or proper noun absent from the evidence block.
3. *Answer*: fixed `answer_context`; assert no price/property/neighbourhood token absent
   from context.

Cost from WS0-D usage **plus the mem0 step** (V4); latency from `RequestTrace`. Runs in CI,
bounded quota. Keep human 1–5 scores as a qualitative column for the production model only.

**Re-baseline `EVAL.md` after WS0 and before WS4** — the existing 6-query table was scored
on 2.0 Flash-Lite while production runs 3.1 Flash-Lite, so it is not a valid baseline to
extend, and a "reranking delta" measured against it would conflate geo aliases, summaries
and reranking.

---

## 11. WS6 — Booking OCR (~4h)

Unchanged. Six fields max. Composes with WS1's trip scope — which, per V3, now actually
holds trip state rather than duplicated traveller facts.

## 12. WS7 — CI (~2h) — first

ruff, pytest, docker build; LLM mocked, zero quota. No `.github/` and no tests exist today.

---

## 13. Risk register (revised)

| # | Risk | L | I | Mitigation |
|---|---|---|---|---|
| R1 | 750 h/month exhausted by a second keep-warm service | H | Main API down | One pinged service only |
| R2 | **512 MB exceeded — worst case 550–700 MB** | **H** | Silent SIGKILL → 502s | WS4 is kept by decision, so the cut is no longer available. Mitigate structurally: **lazy-load the cross-encoder** (§9b), **Redis checkpointer instead of psycopg** (§8.2), no second Postgres driver. Gate with `docker stats` after WS1 and after WS4; if over, feature-flag reranking off in production and on for the EVAL run |
| R2b | **Single vCPU contention** — SSE stream + MCP tool + ONNX inference on one core | M | Visible stalls mid-demo | Don't run the Claude Desktop demo and the web UI concurrently; keep reranking off the streaming path |
| R3 | mem0 pulls torch / falls back to OpenAI | L ✔ | Breaks constraints | Base install is clean ✔; never install `mem0ai[extras]`; `assert_local_and_gemini` at startup |
| R4 | Dependency API drift | H | Silent breakage | Pin `fastmcp==2.14.*`, `mem0ai`, `langgraph`, `google-genai`; re-verify `infer=` |
| R5 | Gemini quota exhausted | M | Demo fails | Two keys, separate projects; Redis cache; **note mem0's calls bypass `llm.py`'s backoff (V4)** |
| R6 | WS0-A at scale blows quota | M | Partial corpus | Top-N subset, offline, snapshot after |
| R7 | Scope: 8 workstreams | H | Nothing finishes well | Cut from the bottom; WS0-C, WS0-F, WS1, WS7 never cut |
| R8 | Qdrant 1 GB | L ✔ | — | ~300–400 MB used; re-embed replaces, not adds ✔ |
| R9 | mem0 `history.db` on ephemeral disk | L | Audit history resets | Document; pin the path |

---

## 14. Schedule (7 days)

| Day | Work |
|---|---|
| 1 | WS7 CI · WS0-B sampler · WS0-D token accounting + provider override |
| 2 | WS0-C service layer · WS0-H summaries path |
| 3 | WS0-F routing · WS0-E geo · WS0-G drift · kick off WS0-A run offline |
| 4 | WS1 memory backend (intent-side dealbreakers, derived trip state, safety) · **RSS gate #1** |
| 5 | WS1 memory panel + localStorage `user_id` · WS2 outbound MCP + screenshot · WS2 inbound weather |
| 6 | WS3 graph + cycles · **Redis checkpointer** |
| 7 | WS3 HITL contract: `awaiting_input` event, resume endpoint, frontend hold-and-resume |
| 8 | WS4 rerank (lazy-loaded) · **RSS gate #2** · WS5 benchmark |
| 9 | WS6 OCR · README v2 · JD_MAPPING · EVAL re-baseline |

Grew 7 → 9 days once the three decisions landed: full interrupt/resume is a day of its own
on top of the graph (new SSE terminal event, resume endpoint, and a real
`ConciergePanel.send()` refactor — it currently assumes one request per turn), and WS4 is no
longer available as the relief valve.

**Never cut:** WS0-C, WS0-F, WS0-H, WS1 + the memory panel, WS7.
**First to cut:** WS6 (OCR), then WS5's third model (benchmark two instead of three), then
WS3's *adjust* branch — keep approve/reject, drop free-text adjustment. **Do not** cut back
to an in-memory checkpointer to save time; it fails exactly across the cold start that HITL
sits in.

---

## 15. Decisions taken

| # | Decision | Consequence |
|---|---|---|
| 1 | **Identity = localStorage UUID** | Same-browser persistence, not identity. README must say so; demo with a reload/new tab, never a private window (§6.6) |
| 2 | **WS4 kept** | The memory relief valve is gone, so reranking must be lazy-loaded and kept off the streaming path, and WS0-H becomes a hard prerequisite (§9) |
| 3 | **Full interrupt/resume HITL** | New SSE terminal event + resume endpoint + frontend refactor; forces a durable checkpointer, which forces **Redis over psycopg** to stay inside 512 MB (§8) |

The three interlock: keeping WS4 and choosing full HITL both consume the same RAM budget,
and the Redis checkpointer is what makes both fit. If that call is ever revisited, §9 and
§8.2 have to be revisited together.

---

## 16. Acceptance

- [ ] WS0: polarity-based sampler with language diversity · real streamed usage + Anthropic
      parity + model override · `app/services/` with no router-private imports · summaries
      collection actually queried · geo aliases resolve "downtown" · `_classify` uses `sq`
      and requires structural signal · `_emit_route_events` offset · one amenity vocabulary ·
      compose mounts `csvData` · model name aligned
- [ ] WS1: dealbreakers extracted in the intent call and applied on the stating turn ·
      deterministic dealbreaker recall · derived trip state · override + disclosure + injection
      defence · `exclude` in cache key and relaxed filter · memory step with token counts
- [ ] WS2: six tools at `/mcp` · 401 unauthenticated · Claude Desktop screenshot · real
      `[r#]` citations through MCP · weather degrades silently when stopped
- [ ] WS3: cycles + per-node SSE preserved · Redis-backed checkpointer survives a container
      restart mid-interrupt (test it by restarting between interrupt and resume) ·
      `awaiting_input` → resume → same `thread_id` continues the run · no psycopg in the
      dependency tree
- [ ] WS4: WS0-H landed · cross-encoder lazy-loaded (cold start that never reranks shows no
      RSS increase) · reranking absent from the concierge streaming path · before/after delta
      measured after the EVAL re-baseline
- [ ] Identity: `user_id` short-hash visible in the memory panel · README states the
      localStorage limitation explicitly
- [ ] WS5: per-stage automated grounding checks in CI · cost table including mem0's calls
- [ ] WS6: six fields with confidence → trip memory
- [ ] WS7: CI green, zero quota
- [ ] README v2 · `JD_MAPPING.md`

## Delivery status — 2026-08-13

Merged to `main` and deployed. Verified end-to-end against the live stack with
`scripts/prod_smoke.py` (~40 content assertions, not status codes).

| Workstream | State |
|---|---|
| WS0 (incl. WS0-A) · debt paydown + LLM summaries | Done, live. 1,286 LLM summaries + `provenance` flag |
| WS1 · memory | Done, live. Dealbreaker chain verified in production end to end |
| WS2 · MCP | Done, live. 401 unauthenticated and on a wrong token; all six tools list |
| WS3 · planner | Done, live. Interrupt reaches `awaiting_input`, resume continues the thread |
| WS4 · reranking | Built and measured, **off** — +156 MB against 33 MB of headroom |
| WS5 · benchmark | Done. Per-stage cost, no LLM judge |
| WS6 · OCR | **Not built** — the plan's designated cut |
| EVAL Q4 / Q6 | Both fixed and verified live |
| Keep-warm | Active since reaching `main`; first run HTTP 200 in 0.154s |

Three things this phase taught that the plan did not anticipate:

1. **A push is not a deploy.** Render and Vercel build the default branch. Two changes sat
   undeployed for hours while looking shipped. Probe for a field the new code adds.
2. **Placeholder numbers get quoted as fact.** The benchmark price table was labelled
   PLACEHOLDER *and* printed every run, and still reached two documents as truth —
   understating cost ~3x and inventing a "4x cheaper" conclusion that did not survive
   correct prices.
3. **LLM spend is a real constraint on verification, and it is MONTHLY.** A day of
   backfills (2,572 calls), three benchmark runs and three end-to-end runs tripped the
   Gemini project's **monthly spending cap** — `RESOURCE_EXHAUSTED: Your project has
   exceeded its monthly spending cap`. Every LLM-dependent check then fails in a way that
   mimics a product regression: empty intent parses, no dealbreakers captured, 0/6 capture
   rates. Two lessons. Read the 429 body before diagnosing anything — a daily quota and a
   monthly cap look identical from the client and only one of them clears overnight. And
   budget spend for verification: an offline backfill is cheap per call and expensive in
   aggregate.

