# EVAL — Agent Output Quality

How agent output quality is measured. A small set of **golden travel queries** with manual scoring. Define these in Phase 3 and use them to test agents as you build (not retrofitted at the end).

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

| # | Query | Expected behaviour | Score (1–5) | Notes |
|---|---|---|---|---|
| 1 | "an entire place in Lisbon under 130 with a balcony for late June" | NL → filters: city=Lisbon, type=`Entire home/apt`, price≤130, amenity=balcony, dates=late June; chips updated (incl. city chip) | — | — |
| 2 | "Find an entire place in Amsterdam near the centre for 3 nights under 200 a night, and tell me what guests consistently praise and complain about." | Full concierge run; ranked candidates + review synthesis (Postgres full-text) with `[r#]` citations to real reviews | — | — |
| 3 | "Plan a 4-night Los Angeles trip for a couple — one stay near the beach and one near Downtown. Budget $1200 total." | Itinerary: 2 stays, day-by-day cards, total ≤ $1200, swap-out alternatives | — | — |
| 4 | "family-friendly place in Amsterdam with a pool and kitchen under 250" | NL → filters: city, amenities=[pool,kitchen], price≤250; real results | — | — |
| 5 | "places in Lisbon guests say are quiet and clean" | review-theme retrieval via summary vectors + FTS; grounded synthesis | — | — |
| 6 | (adversarial) "a castle on the moon under $5" | Honest no-match / closest alternatives, no hallucination, no crash | — | — |

## Results summary

<!-- Fill in after a manual scoring pass: aggregate scores, observed failure modes
     (e.g. non-English aspect sparsity, FTS keyword vs semantic recall), and improvements. -->
*(Pending manual scoring run on the full corpus.)*
