# EVAL — Agent Output Quality

**Last run: 2026-06-19 | Provider: Gemini 2.0 Flash-Lite | Deployment: https://travel-discovery-api.onrender.com (live)**

How agent output quality is measured. A small set of **golden travel queries** with manual scoring.

## Method

- **Golden set:** ~10–15 representative queries spanning the agent surface (NL search parsing, retrieval relevance, review synthesis grounding, itinerary planning), including the two complex brief queries and at least one adversarial/failure case.
- **Scoring (manual, 1–5):** per query, score on the dimensions below. Record provider/model + token usage + latency alongside each.
- **Grounding check:** every factual claim must trace to a retrieved listing/review (citation present and correct). Hallucinated claims = automatic fail. Review synthesis is grounded in **real Inside Airbnb review rows** retrieved via Postgres full-text (focus-ranked) + balanced top/bottom-rated sampling — `[r#]` citations map to real `reviews.id`.

| Dimension | What it measures |
|---|---|
| Intent parsing accuracy | Did the structured query capture city/dates/budget/party/constraints/preferences correctly? |
| Retrieval relevance | Are the ranked candidates actually good matches? (precision@k, eyeballed) |
| Grounding / citations | Every claim cites a real review/listing; no fabrication |
| Synthesis quality | Review summaries faithful, balanced (praise + complaints), useful |
| Itinerary validity | Respects budget, dates, constraints; totals add up; swaps work |
| Failure handling | Graceful degradation / honest "I couldn't find…" rather than confident nonsense |

## Golden queries (real data: Amsterdam · Lisbon · Los Angeles)

Scores are filled in by a manual run against the loaded corpus.

Scores are per-query aggregates across relevant dimensions (intent parsing, retrieval relevance, grounding/citations, synthesis quality, itinerary validity, failure handling). Dimensions not applicable to a query type are omitted from the average.

| # | Query | Expected behaviour | Score (1–5) | Notes |
|---|---|---|---|---|
| 1 | "an entire place in Lisbon under 130 with a balcony for late June" | NL → filters: city=Lisbon, type=`Entire home/apt`, price≤130, amenity=balcony, dates=late June; chips updated (incl. city chip) | **4.5** | Intent parsing perfect (5/5): city, type, price, amenity, dates=June 22–30 all correct. Retrieval (4/5): top result lacks balcony in key_amenities but subsequent results (ROOFTOP Studio, Charming&Central with Balcony) satisfy constraint; 2139 total results. No token/latency trace (NL endpoint). |
| 2 | "Find an entire place in Amsterdam near the centre for 3 nights under 200 a night, and tell me what guests consistently praise and complain about." | Full concierge run; ranked candidates + review synthesis (Postgres full-text) with `[r#]` citations to real reviews | **2.5** | ROUTING FAILURE: router sent query to `itinerary` instead of `review` pipeline — no guest praise/complaint synthesis produced. Answer agent honestly admitted the gap ("context does not contain information regarding guest praise or complaints") — no hallucination. Listing citation present but zero `[r#]` review citations. Tokens: 1012 in / 193 out. Latency: 4953ms. |
| 3 | "Plan a 4-night Los Angeles trip for a couple — one stay near the beach and one near Downtown. Budget $1200 total." | Itinerary: 2 stays, day-by-day cards, total ≤ $1200, swap-out alternatives | **3.5** | Intent (5/5): party=2, budget_total=$1200, 2 constraints extracted correctly. Itinerary (3/5): 2 stays ✓, 4 nights ✓, $402 total well within $1200 ✓, 4 alternatives provided ✓. However "downtown" segment chose Long Beach listings rather than LA Downtown neighbourhood — spatial constraint partially missed. All prices suspiciously identical at $100/night (synthetic data artefact). Tokens: 1013 in / 248 out. Latency: 6617ms. |
| 4 | "family-friendly place in Amsterdam with a pool and kitchen under 250" | NL → filters: city, amenities=[pool,kitchen], price≤250; real results | **3.5** | Intent (4/5): city, amenities=[pool,kitchen], price≤250, vibe=family-friendly all captured; property_types=[] (acceptable). Retrieval (3/5): all 25 results have pool+kitchen and are under $250 ✓, but top 3 results are Private rooms rather than family-appropriate entire homes; "family-friendly" vibe did not bias toward whole-home types. No token/latency trace (NL endpoint). |
| 5 | "places in Lisbon guests say are quiet and clean" | review-theme retrieval via summary vectors + FTS; grounded synthesis | **3.0** | Routing correct (review pipeline) ✓. 3 listings retrieved, 2 review citations returned. Citation IDs present in `data` event (kind=review). Synthesis (2/5): thin — only 2 citations sourced (one French, one English); answer acknowledges no noise-level data found and pivots to cleanliness only. Listing names in synthesis are plausible matches (all named "Quiet …") suggesting FTS keyword match rather than semantic theme retrieval. Tokens: 1215 in / 179 out. Latency: 6970ms. |
| 6 | (adversarial) "a castle on the moon under $5" | Honest no-match / closest alternatives, no hallucination, no crash | **4.0** | No crash ✓. No hallucination ✓ — returned 2 real cheapest-in-corpus listings (€3.62, €3.98 in Lisbon). Budget constraint applied correctly. However "castle" and "on the moon" hard constraints silently dropped with no explicit "I couldn't find…" acknowledgment to user — failure handling (3/5) is partially graceful. City defaulted to null and cross-city search ran, which is reasonable. No token/latency trace (NL endpoint). |

## Results summary

**Run date:** 2026-06-19 | **Provider:** Gemini 2.0 Flash-Lite | **Endpoint:** https://travel-discovery-api.onrender.com (live, free tier — cold-start ~50s before warm)

**Overall verdict: PARTIAL PASS — significant routing defect in Q2, spatial weakness in Q3.**

### Aggregate scores

| Query | Score |
|---|---|
| Q1 — Lisbon NL balcony | 4.5 |
| Q2 — Amsterdam concierge review synthesis | 2.5 |
| Q3 — LA 4-night itinerary | 3.5 |
| Q4 — Amsterdam family pool+kitchen | 3.5 |
| Q5 — Lisbon quiet & clean review theme | 3.0 |
| Q6 — Adversarial castle/moon | 4.0 |
| **Average** | **3.5 / 5.0** |

### Token and latency (concierge queries only)

| Query | Input tokens | Output tokens | Latency ms |
|---|---|---|---|
| Q2 | 1012 | 193 | 4953 |
| Q3 | 1013 | 248 | 6617 |
| Q5 | 1215 | 179 | 6970 |

### What worked

- NL intent parsing is strong (Q1, Q4): city, property type, price cap, amenities, and date windows all extracted correctly from natural language.
- Itinerary budget compliance: Q3 total ($402) correctly within $1200, correct 2-segment structure with alternatives.
- Grounding honesty: agents refused to fabricate when context lacked data (Q2 review gap, Q5 noise data gap). No hallucinations observed.
- Adversarial robustness: Q6 did not crash, did not invent a "moon castle" — fell back to real listings matching only the price constraint.

### Observed failure modes

1. **Routing failure (high severity — Q2):** A query explicitly requesting both retrieval AND review synthesis was routed entirely to the `itinerary` pipeline, producing zero review citations. The router needs a combined `retrieval+review` route or composite-intent detection for multi-ask queries.
2. **Spatial constraint miss (medium severity — Q3):** The "near Downtown LA" constraint resolved to Long Beach neighbourhood listings rather than actual Downtown Los Angeles. Neighbourhood-to-query semantic match is too loose; geo-bounding or neighbourhood whitelist for "downtown" should be added.
3. **FTS keyword dominance over semantic recall (medium severity — Q5):** Lisbon "quiet and clean" retrieval surfaced only listings literally named "Quiet …" (3 of 3 matches). Semantic theme detection via review embeddings does not appear to be operating — results look like FTS keyword hits on listing names, not review-content vectors.
4. **Amenity-type mismatch in retrieval ranking (low severity — Q4):** Family-friendly filter returns Private rooms as top results. A family-friendliness signal should down-rank private rooms in favour of Entire home/apt when the vibe is family.
5. **Silent constraint dropping on impossible queries (low severity — Q6):** "castle" and "on the moon" hard constraints were silently ignored without user-facing explanation. The answer agent should explicitly state which constraints were unresolvable.

### Recommended improvements (prioritized)

1. **[Critical] Add composite routing for multi-intent queries** — detect when a query requests both a listing recommendation AND review synthesis, and run both sub-pipelines, merging outputs before the answer step.
2. **[High] Add geo/neighbourhood disambiguation for "downtown" and landmark references** — map known place-name aliases to bounding boxes or neighbourhood IDs to prevent spatial drift.
3. **[High] Verify review-embedding retrieval is active for theme queries** — Q5 suggests FTS is winning over vector retrieval for vibe/theme queries; confirm Qdrant review-summary vectors are indexed and that the review agent uses them.
4. **[Medium] Constrain family-friendly results to Entire home/apt** — when vibe includes "family" without an explicit room-type preference, bias retrieval toward whole-unit listings.
5. **[Low] Surface unresolvable constraints in the answer** — when hard constraints are dropped (impossible city, nonsense amenity), the answer agent should acknowledge them explicitly rather than silently omitting.
