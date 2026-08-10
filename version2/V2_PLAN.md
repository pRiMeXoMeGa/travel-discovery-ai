# Travel Discovery AI v2 — Workstream Specs

**Thesis:** v1 answered travel questions. v2 makes the platform agentic — it remembers
who you are, calls out to external tools, and exposes itself as a tool other agents use.

Frame v2 in the README as **delivering v1's own "What I'd do with another week" roadmap**
where it genuinely does. That is a far stronger story than "I added features for an
interview" — but check it against the real list before claiming it.

v1's actual four bullets, and what v2 does about each:

| v1 roadmap bullet | v2 status |
|---|---|
| Embed all 200K reviews for per-review semantic search | **Partially** — WS4 reranking mitigates the recall trade-off without the GPU run. Say "mitigated", not "delivered" |
| Move aspect sentiment + per-property summaries to an LLM | **Unclaimed by any workstream.** This is also `FINDINGS.md` 2.1: the shipped "AI Review Summary" is a heuristic that concatenates two truncated review quotes. Highest-value item on the list and currently missing from v2 — consider adding it |
| Single always-on VM instead of free-tier PaaS | Not planned for v2 |
| Materialize the calendar / migrate `.search` → `query_points` | **Partially** — the working rule "new collections use `query_points`" covers new code only; the existing `retrieval.py` `.search` calls stay |

WS5 (benchmark) and WS6 (OCR) appear **nowhere** in v1's roadmap — they are JD-driven
additions. Justify them on product merit, not as roadmap delivery.

---

## WS1 — mem0 traveller + trip memory ★ core · ~1 day

Travel is a genuinely strong memory use case: preferences are stable, numerous, personal,
and **revealed conversationally rather than filled into a form**. That last point is what
mem0's extraction is actually for.

### Scopes

| Scope | Key | Holds |
|---|---|---|
| Traveller | `user_id` | budget band, room type, neighbourhood vibe, must-have amenities, **dealbreakers**, party composition, accessibility |
| Trip | `run_id="trip::<id>"` | cities, dates, constraints set this session, existing bookings |

### Where memory enters the pipeline

1. **Before the intent agent** — retrieve traveller memories, inject as context so
   `StructuredQuery` inherits known preferences the user didn't restate.
2. **Into retrieval as payload filters** — dealbreakers become *hard* Qdrant filters, not
   soft prompt hints. "Never show me shared rooms" must actually exclude them. Only
   dealbreakers that map onto a real payload field can do this — see the mapping rules in
   CLAUDE.md.
3. **After the response streams** — write to both scopes, awaited with an 8s cap rather
   than fire-and-forget, so the memory panel can show what was extracted on the same turn.
   It runs after the answer has fully streamed, so it adds no perceived latency.

### The demo moment

Session 1: *"Lisbon, I hate stairs and I need fast wifi for work."*
Session 2, fresh session, same user: *"3 nights in Amsterdam in October."*
Results are pre-filtered for lift access and wifi. The memory panel shows which memories
fired, with scores.

Both of those map cleanly onto the real schema — `elevator` and `wifi` are in the canonical
18-term amenity vocabulary, so they become genuine payload filters.

Dealbreakers are the sharpest version — a memory that visibly changes results forever
after is understood in one second. Use **"never show me shared rooms"** (`type` must_not
`Shared room`). The earlier draft said "shared bathrooms", which has no payload field in
this corpus and cannot be filtered — see CLAUDE.md, "Dealbreakers map only to fields the
schema actually has".

### UI — the memory panel

Non-negotiable. Shows per turn: memories **retrieved** with similarity scores, memories
**written**, and a **forget** button per memory. An invisible memory layer is
indistinguishable from no memory layer.

**Placement:** a collapsible *Memory* section inside the existing concierge slide-over
(`frontend/components/concierge/ConciergePanel.tsx`, the 440px right panel), above the
agent step trail. That panel is mounted globally in `app/providers.tsx`, so the memory
layer is reachable from every route without adding a pane to the results page or squeezing
the 42% map.

### Seeding

Seed ~15 memories by replaying simulated prior conversations through `mem0.add()` — never
by writing vectors directly. Provenance stays inspectable and the transcripts live in the
repo. Run **once, offline**, then snapshot Qdrant. Extraction costs ~2 Gemini calls per
memory; embedding is free.

### Risks

- mem0 silently falls back to OpenAI when a provider is omitted → `assert_local_and_gemini`
- Dim mismatch → opaque Qdrant shape error. Collection must be 384.
- Query-prefix inconsistency (`query_embed` vs `embed`) degrades recall **without raising**.
  *Checked: `backend/app/embeddings.py` uses plain `.embed()`, so the shim already matches —
  do not add a prefix.*
- mem0 reads `GEMINI_API_KEY` from `os.environ`; pydantic-settings does not export it.
  Fine under docker-compose, silently unset under a bare local `uvicorn`.
- `mem0` is in no requirements file yet, and adding it must not drag in torch —
  its `huggingface` embedder does. Pin and check the resolved tree.

---

## WS2 — MCP, bidirectional ★ core · ~0.5 day → revise to ~1 day

**Full spec: `version2/WS2_MCP.md`. Skeleton: `version2/server.py`
(destination `backend/app/mcp_server/server.py`).**

Still the highest impact per hour on this list, but the 0.5-day estimate predates two
things the code review turned up:

- **Part 0, the service-layer extraction (~1.5h).** `orchestrator.py:63` imports private
  helpers from `routers/search.py`, and `routers/agents.py` imports a sibling router plus
  `retrieval._parse_constraints`. Six MCP tools on top of that makes it permanent.
- **Four of the six tools are not pure wraps.** `compare_listings` currently always spends
  up to 5 LLM calls on the verdict; `synthesize_reviews` has no abstention flag;
  `check_availability` needs a range→days helper; `min_rating` is unenforceable on the
  semantic path without a new Qdrant payload index and re-snapshot. Details in WS2_MCP.md.

Budget a full day. It is still worth it — but "wiring" undersells it.

Most portfolios wrap their own internal functions in MCP and call it integration. A
reviewer sees through that immediately — why would you need a protocol to reach your own
database? Do it where the boundary is real, in both directions.

### Outbound — expose the platform as an MCP server

Travel Discovery becomes a tool other agents can use. Own process, streamable HTTP,
Pydantic schemas on every tool. Your service layer already exists, so this is wiring.

Tools: `search_listings` · `get_listing_detail` · `synthesize_reviews` ·
`compare_listings` · `check_availability` · `plan_itinerary`

**What makes it differentiated:** `synthesize_reviews` returns grounded output with
mandatory `[r#]` citations to real review rows, because it's backed by the existing
review-intelligence agent — not a hallucinated summary. Put that in the tool description
so a calling agent knows what it's getting.

Fix two things first, or the description overpromises (both from `FINDINGS.md`): the
snippet sampler falls back to `ORDER BY rating` on a column that is **always NULL**, so
its "balanced top/bottom" selection is a no-op (order by `sentiment` instead), and the
corpus averages **~4 reviews per listing**, so pick high-`review_count` listings for the
screenshot.

Note `search_listings`, `get_listing_detail`, `compare_listings`, `synthesize_reviews` and
`plan_itinerary` all map onto existing endpoints or agent functions, so those really are
wiring. **`check_availability` does not** — availability is a pure function
(`backend/app/availability.py::is_available_range`) plus a 30-day window embedded in the
detail response. Budget it as new plumbing.

**Deliverable:** screenshot of **Claude Desktop planning a trip through your MCP server**
→ `version2/img/`. Very few applicants will have this.

### Inbound — consume an external MCP server

Weather is the honest fit: a planner that checks the forecast for the actual travel dates
is a better planner. Closes the JD's "integrate agents with external APIs and third-party
services."

Must degrade gracefully when the external server is unreachable — and it *will* be
unreachable, by design: Render's 750 instance-hours/month are per account, so a second
keep-warm service would exhaust the allowance and take the main API down with it. Run
weather un-pinged and let the 3s timeout degrade silently, or run it in docker-compose
only. That constraint makes the failure path part of the demo rather than an excuse.

---

## WS3 — LangGraph trip planner · ~1 day

**Do not port the 4-agent concierge.** v1's rationale for the custom orchestrator is
correct and stays in the README.

Add LangGraph for a new flow the current DAG genuinely cannot express:

- **Cycles** — budget exceeded → replan; no availability → alternatives
- **Conditional routing** — preference conflict → clarification node
- **HITL interrupt** — present the plan, wait for approve/adjust before committing
  (reinforces the HITL claim already on the CV)
- **Checkpointer** — state persists across turns, feeding trip memory

Keep two properties LangGraph does not give you free: **per-node SSE step events**, and
**per-node error guards that degrade the stream rather than crash it**.

**The interview line:** *"Four cooperating agents in a fixed sequence didn't need a graph,
so the concierge uses a custom async-generator orchestrator for tighter SSE step
accounting. A planner with replanning cycles and a human checkpoint is graph-shaped, so
it uses LangGraph."* Write it down in the README before the interview.

---

## WS4 — Cross-encoder reranking · ~2–3 hours

Best ratio on the list. The JD says *chunking, embedding, and **reranking***. You have
two of three.

fastembed ships ONNX cross-encoder reranking (`TextCrossEncoder`) — no torch, fits 512 MB.
Retrieve top-50 from Qdrant, rerank to top-10.

**Verify the pin first.** `backend/requirements.txt` pins `fastembed==0.4.*`; confirm that
range actually exposes `TextCrossEncoder` and bump it if not. Also check the added RSS on
a 512 MB box — a second ONNX model is loaded alongside bge-small, and v1 already runs a
single uvicorn worker specifically to avoid duplicate model loads.

Bonus: partially mitigates the documented semantic-recall trade-off on reviews, so you
**update** that trade-offs section rather than restating it. Measure before/after on the
`EVAL.md` golden set and put the delta in the README.

Caveat on the ceiling: v1 embeds *listings + per-property summaries*, not individual
reviews, and those summaries are heuristic quote-concatenations (`FINDINGS.md` 2.1).
Reranking improves ordering over that corpus; it cannot recover semantics the summaries
never encoded. Fixing the summaries first would raise the ceiling reranking works against.

---

## WS5 — Model benchmark harness · ~3 hours

The JD asks to *"benchmark models and recommend optimal LLM choices based on cost,
latency, and accuracy."* You are one script away.

You already have: `EVAL.md` golden queries, per-step token/latency accounting in
`observability.py`, a provider switch in `llm.py` (`settings.llm_provider`), and a measured
cost-per-query analysis. All four verified present.

**One real gap to close before the numbers mean anything.** Token accounting is exact for
non-streaming calls (`complete_json_with_usage` / `complete_text_with_usage` read Gemini's
`usageMetadata` and Anthropic's `usage`), but the **answer step is not measured** —
`llm.stream_text()` yields text only, and `orchestrator.py:141` counts
`output_tokens += 1` per chunk as an explicit "coarse token proxy". The answer is one of
the two largest calls in a turn, so a cost table built on the current trace would be wrong.
Fix by parsing `usageMetadata` from the final SSE frame of `streamGenerateContent` (and
`message_delta.usage` for Anthropic) before benchmarking.

Also note the golden set is currently **6 queries**, though `EVAL.md`'s own Method section
says 10–15. Expand it or correct the method text — a benchmark across three models makes
the discrepancy conspicuous.

Run the golden set across Flash-Lite / Flash / Haiku → emit a table of cost per query,
p50/p95 latency, and grounding-check pass rate → commit to `EVAL.md` with a recommendation
and its reasoning.

Turns four things you already built into one artifact matching a JD bullet nearly verbatim.

---

## WS6 — Booking-document OCR · ~4 hours

The JD names *document OCR workflows for unstructured content (PDFs, images, scanned
docs)*, and structured extraction with confidence scoring is already on the CV.

Flow: upload an existing booking confirmation (PDF or phone photo) → Gemini multimodal
OCR → Pydantic structured extraction **with per-field confidence** → merged into trip
memory → the itinerary agent plans *around* the booking you already have.

**Scope discipline:** six fields maximum (property, city, check-in, check-out, guests,
total). Scope creep here is the main risk on this workstream. It composes with mem0 rather
than needing new storage.

---

## WS7 — CI · ~2 hours

GitHub Actions: ruff/lint, pytest, docker build. Mock the LLM in tests — the suite must
consume zero quota. Closes JD section 4's CI/CD line.

---

## Schedule

| Day | Work |
|---|---|
| 1 | WS1 backend + embedder shim + memory panel |
| 2 | **WS2 Part 0 service-layer extraction** + outbound MCP + Claude Desktop screenshot |
| 3 | WS2 inbound weather · WS4 reranker · WS7 CI |
| 4 | WS3 LangGraph planner with cycles + HITL |
| 5 | WS5 benchmark · WS6 OCR · README v2 · JD_MAPPING |

Rebalanced from four days to five: WS2 grew from 0.5 to ~1 day once the service-layer
extraction and the four non-wrap tools were costed. Day 2's refactor is also the enabler
for WS3 and WS5, so it is not lost time.

**Cut from the bottom.** If day 4 slips, drop WS6 before WS3 — LangGraph is in the JD's
first bullet. Never cut the memory panel. WS2 Part 0 is never cut either: it is the one
piece of the plan that improves v1's architecture whether or not the rest ships.

---

## Deliverables checklist

- [ ] `version2/JD_MAPPING.md` — four rows, one per JD section, each → a real file path
- [ ] `version2/img/mcp-claude-desktop.png`
- [ ] README v2: memory architecture, MCP both directions, both orchestration approaches
      with rationale, updated trade-offs, updated known limitations, reranking delta,
      benchmark table, revised cost-per-query, hours spent
- [ ] `EVAL.md`: memory-aware golden queries, reranking before/after, model benchmark

## Interview lines

- Memory is scoped to traveller *and* trip; dealbreakers become hard retrieval filters,
  not prompt suggestions.
- The platform is both an MCP client and an MCP server — here's Claude Desktop booking
  through it, with grounded citations coming back.
- Two orchestration approaches, chosen per problem shape, both documented.
- Embeddings run locally at 384-dim: zero marginal cost per query, fits a 512 MB host.
- 50K real listings, 200K real reviews. Not a toy corpus.
