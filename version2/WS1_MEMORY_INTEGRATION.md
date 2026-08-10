# WS1 — wiring memory into `run_concierge`

Written against the actual `agents/orchestrator.py`. Four hook points. The existing
event vocabulary already carries everything memory needs — **no new SSE event type**.
The memory panel filters on `agent == "memory"`.

## Hook 1 — recall, before the intent step

Insert immediately after `trace = RequestTrace(...)`, before the intent block.

```python
yield {"type": "step", "agent": "memory", "status": "start",
       "data": {"phase": "recall"}}
mem_step = AgentStep("memory", "start")
t_mem = time.perf_counter()

memories = await store.recall(req.query, user_id=req.user_id,
                              trip_id=req.trip_id)

mem_step.status = "done"
mem_step.data = {"phase": "recall",
                 "traveller": memories["traveller"],
                 "trip": memories["trip"]}
mem_step.latency_ms = (time.perf_counter() - t_mem) * 1000
trace.add(mem_step)
yield {"type": "step", "agent": "memory", "status": "done", "data": mem_step.data}
```

`store.recall` never raises — it returns empty lists on failure, matching the
degradation contract the rest of the orchestrator already honours.

Each item carries `score`, so the panel can show *why* a memory fired. Zero LLM calls:
recall is a local embed plus a Qdrant query.

`ConciergeRequest` gains `user_id: str` and `trip_id: str | None`.

## Hook 2 — inject into the intent agent

```python
sq = await intent.parse_intent(req.query, step=step,
                               memory_context=store.as_prompt_context(memories))
```

In `intent.py`, append `memory_context` to the system prompt above the user request.
Keep the existing no-fabrication rule and add: *the current request wins where it
contradicts a remembered preference.* Without that line, a remembered €80 budget will
quietly override an explicit "splurge tonight".

Same call count — context is added to a call that already happens.

## Hook 3 — dealbreakers as hard filters

```python
dealbreakers = store.extract_dealbreakers(memories)
# -> {"must": [{"field": "amenities", "value": "elevator"}],
#     "must_not": [{"field": "type", "value": "Shared room"}],
#     "unmapped": ["no shared bathrooms"]}
candidates = await retrieval.retrieve(sq, limit=10, exclude=dealbreakers)
```

`must` / `must_not` drop straight into `_build_qdrant_filter`'s existing `must` and
`must_not` lists as `FieldCondition(key=field, match=MatchValue(value=value))`.
`unmapped` goes to the prompt as a soft preference — never silently discarded.

The real signature today is `retrieve(sq: StructuredQuery, limit: int = 20)` — `exclude`
is a new keyword argument, and `limit` is capped at `_HARD_CAP = 50`.

**This is the part that must not become a prompt hint.** In `retrieval.retrieve`,
dealbreakers join the existing hard-constraint payload filter set built by
`_build_qdrant_filter`, alongside the `type` filter already there (from `_TYPE_KEYWORDS`).
They go in as `must_not` conditions. A dealbreaker in a prompt is a suggestion the model
may ignore; a payload filter is a guarantee — which is what the user meant by "never show
me this again".

**A dealbreaker can only reference a field the payload has.** `extract_dealbreakers`
therefore returns *validated conditions*, not free text — the 18-term amenity vocabulary
and the four real room types (see CLAUDE.md). `elevator`, `wifi`, `pets_allowed`,
`Shared room` all work. "No shared bathrooms", "not near a highway", "nothing above the
third floor" have no field and cannot be filtered; they fall through to the prompt as soft
preferences. Surface that distinction in the memory panel — a dealbreaker the user
believes is enforced but isn't is worse than none.

**Direction comes from write-time metadata, not from read-time inference.** Each
dealbreaker is stored as `{kind, field, value, op}` and validated by
`store.validate_dealbreaker()` when written. Two reasons this cannot be derived later:
polarity is not a property of the field (`pets_allowed` means *pets are permitted* — an
allergy sufferer needs `must_not` where a dog owner needs `must`), and scanning the memory
text for vocabulary terms misfires on *"the elevator was broken, avoid this place"*, which
would map to **require elevator**. Read-time projection is therefore deterministic and
costs no LLM calls.

If a mapping ever needs a payload field outside the current set, it also needs a Qdrant
payload index (`scripts/ensure_qdrant_indexes.sh`, six fields today) and a re-snapshot —
Qdrant Cloud strict mode 400s on unindexed filter fields.

Only dealbreakers become filters. Soft preferences (vibe, neighbourhood feel) stay in
the prompt, where ranking can weigh them.

## Hook 4 — write, after the answer finishes streaming

Between the `answer_step` block and the final `done` event. Accumulate the streamed
answer while emitting tokens:

```python
answer_parts: list[str] = []
async for tok in llm.stream_text(prompt, system):
    answer_parts.append(tok)
    yield {"type": "token", "text": tok}
```

Then:

```python
yield {"type": "step", "agent": "memory", "status": "start",
       "data": {"phase": "write"}}
written = await store.remember(req.query, "".join(answer_parts),
                               user_id=req.user_id, trip_id=req.trip_id)
yield {"type": "step", "agent": "memory", "status": "done",
       "data": {"phase": "write", "written": written}}
```

**Await rather than fire-and-forget, deliberately.** The write costs Gemini calls and
lands after the user already has their full answer, so it adds no perceived latency to the
result — but the panel gets to show what was actually extracted. A background task would
leave the panel empty until the next turn, and an invisible memory layer is
indistinguishable from no memory layer. `store.remember` caps itself at 8s and swallows
its own failures, so a hung write cannot stall the `done` event.

**Writes go to both scopes, from one extraction.** `remember()` runs a single inferred
`add()` into `user_id`, then mirrors the already-extracted facts into `trip::<id>` with
`infer=False`. Writing only to the trip scope — as the first draft of `store.py` did —
means no traveller preference is ever learned during a trip session, which is exactly the
demo. Calling `add()` twice would instead cost double *and* be non-deterministic: two
extractions of the same turn can disagree, leaving the scopes silently divergent.

## Call budget

| Step | Gemini calls |
|---|---|
| Memory recall | **0** — local embed + Qdrant |
| Router `_classify` | **0** — already keyword-based |
| Intent | 1 (unchanged, richer context) |
| Route runner | 0–1 (unchanged) |
| Answer | 1 (unchanged) |
| Memory write — traveller | 1–2 (inferred extraction) |
| Memory write — trip mirror | **0** (`infer=False`) |

Net increase: **1–2 calls per turn, whether or not a trip is active** — the trip mirror is
free, so trip state no longer changes the budget.

Totals, honestly:

| Route | Before memory | After memory |
|---|---|---|
| `search` (route runner makes no LLM call) | 2 | 3–4 ✅ |
| `review` / `itinerary` (route runner makes 1) | 3 | 4–5 ⚠️ |

So CLAUDE.md's "≤ 4 per turn" holds everywhere except a review/itinerary turn whose
extraction takes two calls, which peaks at **5**. That is one over, not the three-over the
double-extraction design would have been (7). Either state the ceiling as "≤ 5 on
planning turns" or cap extraction at one call — but do not leave the stated ceiling
contradicting the measured worst case.

Dealbreaker projection adds nothing — it reads validated metadata.

Recall is free because embeddings are local — worth stating in the README, since most mem0
integrations pay per-turn API embedding costs on both read and write.

## Frontend

A collapsible **Memory** section inside the existing concierge slide-over
(`frontend/components/concierge/ConciergePanel.tsx` — the 440px right panel), above the
agent step trail. That component is mounted globally in `frontend/app/providers.tsx`, so
memory is visible from every route without adding a column to the results page, which is
already sidebar + list + 42% map.

It is fed by the same SSE stream the panel already consumes via
`streamConcierge()` in `frontend/lib/concierge.ts` — add a `memory` branch alongside the
existing `step` / `data` / `itinerary` / `token` / `done` handling in `send()`:

```ts
if (ev.type === "step" && ev.agent === "memory") {
  if (ev.data?.phase === "recall" && ev.status === "done") setRecalled(ev.data);
  if (ev.data?.phase === "write"  && ev.status === "done") setWritten(ev.data.written);
}
```

Two details the existing panel forces:

- The step-trail loop pushes every non-`router` step into `t.steps` and labels it from
  `STEP_LABEL`, which has no `memory` key — it would render the raw string `memory` twice
  (recall and write collide on `steps.findIndex(s => s.agent === ev.agent)`). Either add a
  `memory` label and key the lookup on `phase`, or skip `memory` in the trail the way
  `router` is skipped and render it only in the new section.
- `ConciergeEvent`'s `step` variant types `data` as `unknown`, so `ev.data?.phase` needs a
  narrowing type or a declared memory payload in `lib/concierge.ts`.

Render: retrieved memories with scores, badge dealbreakers distinctly, badge unmappable
dealbreakers as *soft* so the user is not told something is enforced when it isn't, written
memories with a `new` marker, and a forget button per item calling `DELETE /api/memory/{id}`
→ `store.forget`.

## Demo script

1. Session 1 — *"Lisbon in June, I hate stairs and need fast wifi for work."*
   Panel shows two memories written.
2. Session 2, fresh session, same `user_id` — *"3 nights in Amsterdam in October."*
   Panel shows both recalled with scores; results are pre-filtered for lift access
   and wifi (`elevator` and `wifi` are both in the canonical amenity vocabulary, so these
   are real payload filters). **This is the demo's opening moment.**
3. *"Never show me shared rooms again."* → next search visibly excludes them
   (`type` must_not `Shared room`). The dealbreaker-as-hard-filter moment, understood in
   one second. *(Do not use "shared bathrooms" — no such payload field exists in this
   corpus, so it cannot be filtered.)*
4. Hit forget on a memory, re-run — the behaviour reverts. Proves it's real state,
   not a scripted demo.

## Two small things noticed while reading orchestrator.py

Both confirmed against the current file.

- `_run_itinerary` (`orchestrator.py:225`) is annotated `-> tuple[str, list[dict]]` but
  returns three values on both its success and fallback paths; the caller unpacks three.
  Annotation should be `tuple[str, list[dict], dict | None]`.
- `_classify(query, sq)` (`orchestrator.py:41`) never uses `sq`. Either drop the parameter
  or use it. Note `StructuredQuery` has **no `nights` field** — it carries `check_in` /
  `check_out` / `party_size`, so nights would be derived as
  `(sq.check_out - sq.check_in).days`, the same way `itinerary._trip_nights` already does it.

Worth fixing in the same PR. Reviewers who read the code notice annotation drift, and
this repo is otherwise tidy enough that it stands out.

While in here, note the routing defect this WS sits next to: `_classify` returns exactly
one route and `"nights"` is in `_PLANNING_KEYWORDS`, so a query asking for stays *and*
review synthesis routes wholly to `itinerary` and returns zero `[r#]` citations. That is
`EVAL.md`'s highest-severity finding and `FINDINGS.md` 2.4 — and using `sq` in `_classify`
is a natural moment to fix it.
