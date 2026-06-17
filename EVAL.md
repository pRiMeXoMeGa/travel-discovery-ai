# EVAL — Agent Output Quality

How agent output quality is measured. A small set of **golden travel queries** with manual scoring. Define these in Phase 3 and use them to test agents as you build (not retrofitted at the end).

## Method

- **Golden set:** ~10–15 representative queries spanning the agent surface (NL search parsing, retrieval relevance, review synthesis grounding, itinerary planning), including the two complex brief queries and at least one adversarial/failure case.
- **Scoring (manual, 1–5):** per query, score on the dimensions below. Record provider/model + token usage + latency alongside each.
- **Grounding check:** every factual claim in an agent answer must trace to a retrieved listing/review (citation present and correct). Hallucinated claims = automatic fail on grounding.

| Dimension | What it measures |
|---|---|
| Intent parsing accuracy | Did the structured query capture city/dates/budget/party/constraints/preferences correctly? |
| Retrieval relevance | Are the ranked candidates actually good matches? (precision@k, eyeballed) |
| Grounding / citations | Every claim cites a real review/listing; no fabrication |
| Synthesis quality | Review summaries faithful, balanced (praise + complaints), useful |
| Itinerary validity | Respects budget, dates, constraints; totals add up; swaps work |
| Failure handling | Graceful degradation / honest "I couldn't find…" rather than confident nonsense |

## Golden queries

<!-- TODO: fill in during Phase 3. Template below. -->

| # | Query | Expected behaviour | Score (1–5) | Notes |
|---|---|---|---|---|
| 1 | "a quiet 1-bed in Lisbon under 130 with a balcony for late June" | NL → filters: city=Lisbon, beds=1, price≤130, amenity=balcony, dates=late June; chips updated | — | — |
| 2 | "Find me a quiet 1-bedroom in Lisbon near good restaurants for 3 nights in late June, under 130 a night, balcony if possible, no party-type buildings, and tell me which one has the most consistent reviews." | Full concierge run; ranked candidates + review-consistency verdict with citations | — | — |
| 3 | "Plan a 4-night Dubai trip for a couple, one mid-range hotel near the metro and one splurge night with a view. Budget AED 4000 total. Avoid Deira." | Itinerary: 2 stays, day-by-day, total ≤ AED 4000, excludes Deira | — | — |
| 4 | (adversarial) query with no possible match | Honest "no results / closest alternatives", no hallucination | — | — |

## Results summary

<!-- TODO: aggregate scores, observed failure modes, and what you'd improve. -->
