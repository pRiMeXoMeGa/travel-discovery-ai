# Features

Everything this project does, in plain English. No jargon, no steps — just what it can do.

For *how to demo* each of these, see [DEMO.md](./DEMO.md).

---

## 1 · The booking product

1. **Search 50,000 real properties** across Amsterdam, Lisbon and Los Angeles.
2. **Filter** by city, price range, minimum rating, property type and amenities.
3. **Sort** by price, rating, popularity, or distance from a point (real haversine maths in SQL).
4. **Paginate** results.
5. **Interactive map** with a price label on every pin.
6. **Pin clustering** that splits apart as you zoom in.
7. **"Search this area"** — move the map, click the button, results refilter to what you're looking at.
8. **Hover a card, its pin highlights** (and vice versa).
9. **Listing detail page** — photo gallery, amenities, host info, star rating, review count.
10. **Every listing has at least 4 photos**, built from real Airbnb image URLs.
11. **30-day availability calendar** on each listing.
12. **Price breakdown** for a chosen date range.
13. **Neighbourhood price percentile** — "priced in the 42nd percentile for Jordaan", so you know if it's cheap *for that area*.
14. **Browse reviews** with paging (20 at a time).
15. **Filter reviews by language** — the corpus is genuinely multilingual.
16. **Search review text by topic** (e.g. only reviews mentioning "location").
17. **Wishlist** — heart any listing, it persists across page reloads.
18. **Compare 2–4 listings** side by side in a matrix.
19. **AI verdict** on a comparison — a written recommendation grounded in each property's reviews.
20. **Correct currency per city** — € for Amsterdam and Lisbon, $ for Los Angeles, never converted.
21. **Filter chips** showing what's currently applied, each removable.

## 2 · The data underneath

22. **Real Inside Airbnb data** — 50,000 listings and 200,000 reviews, not synthetic.
23. **Re-runnable ingestion pipeline** that rebuilds everything from the raw CSVs.
24. **Price cleaning** — `$1,234.00` becomes a number, missing values median-imputed.
25. **Amenity normalisation** — messy free text collapsed into 18 standard terms.
26. **Language detection** on every review.
27. **Aspect sentiment** — each review scored for cleanliness, location, value, noise and staff.
28. **Per-property review summaries** for all 50,000 properties.
29. **1,286 genuinely AI-written summaries** for the properties with the most review evidence.
30. **Honest labelling of summaries** — real AI ones get a sparkle icon and "AI Review Summary"; the rest say "What guests said · quoted from reviews". The label always tells the truth.
31. **Deterministic availability calendar**, generated consistently rather than randomly.
32. **Semantic search index** — listings and summaries embedded as vectors, computed locally with no paid API.

## 3 · Natural language & the concierge

33. **Natural-language search bar** — type a sentence, get filters.
34. **Understands dates in plain English** — "late June" becomes real check-in/check-out dates.
35. **Extracts city, budget, party size, amenities, property type and area** from a sentence.
36. **Shows what it understood** as chips you can see and correct.
37. **Says what it could NOT do** — ask for "a castle on the moon" and it tells you it dropped "castle" and "on the moon" instead of silently ignoring them.
38. **Family requests prefer whole homes** — asking for a "family-friendly" place ranks entire homes above single rooms, without hiding the rooms.
39. **Understands neighbourhoods by nickname** — 161 aliases so "near the centre" or "downtown" map to the real neighbourhood names, per city.
40. **Streaming AI concierge** — answers appear word by word.
41. **Live step timeline** — you watch it think: intent → retrieval → review analysis → answer.
42. **Handles two questions at once** — "find me a place AND tell me what guests complain about" runs both pipelines and merges the results.
43. **Review synthesis grounded in real reviews** — every claim cites an actual review row, with `[r1]`-style references.
44. **Admits when it doesn't know** — a property with no reviews gets "I don't have evidence", not an invented summary.
45. **Multi-stop trip planning** — "4 nights in LA, one stay near the beach and one downtown" produces a real plan.
46. **Budget awareness** — the plan totals the cost and tells you whether it fits.
47. **Resists prompt injection** — asked to reveal its system prompt, it declines *and still answers the real question*.
48. **Graceful failure** — if search breaks, it says so honestly rather than making something up.

## 4 · Memory (it remembers you)

49. **Remembers preferences between sessions** — close the tab, come back, it still knows.
50. **Standing rules become hard filters** — "never show me shared rooms again" is enforced on every future search.
51. **Understands direction correctly** — "I'm allergic to dogs" and "I always travel with my dog" set opposite rules on the same field.
52. **Tells you when it used your memory** — the answer says which saved preferences were applied.
53. **A "Memory" panel** showing what it knows, with badges like `never · Shared room`.
54. **Forget button** on every memory — click, and behaviour reverts immediately.
55. **Revoke by talking** — "actually shared rooms are fine now" removes the rule.
56. **This turn beats memory** — a remembered €80 budget doesn't apply if you say "splurge tonight".
57. **Memory can never set city, dates or budget** — those are hard filters, and letting old text drive them would hijack your search.
58. **Trip-specific memory** kept separate from long-term preferences.

## 5 · Human-in-the-loop planner

59. **Plan mode** — a separate assistant mode with its own workflow.
60. **It stops and asks for approval** before finalising a plan.
61. **Approve or Adjust** — say "somewhere quieter" and it re-plans, then asks again.
62. **Survives a server restart** — the paused plan is saved, so restarting the backend mid-decision doesn't lose your trip.
63. **It can loop** — replan, re-check budget, come back, up to a sensible limit.
64. **Budget relaxation is disclosed** — if it has to loosen your budget to find anything, it says so.

## 6 · Tool use, both directions

65. **The platform is an MCP server** — other AI agents (like Claude Desktop) can use it as a tool.
66. **Six tools exposed**: search listings, get details, check availability, compare, synthesise reviews, plan an itinerary.
67. **Password-protected** — no key or a wrong key gets rejected, and it fails *closed*.
68. **Rate-limited** so an external agent can't burn your whole AI quota.
69. **Cheap tools stay cheap** — the tools an agent browses with cost zero AI calls by design.
70. **The platform also consumes an external tool** — it calls a third-party weather service to add forecasts to trip plans.
71. **If that weather service is down, the trip plan still works** — it just skips the forecast.

## 7 · Engineering you can see

72. **Full request tracing** — every answer reports which steps ran, how long it took, and how many tokens it used.
73. **Token counts are measured**, taken from the AI provider, not guessed.
74. **Caching** — repeat searches return instantly; your filtered results are never served to someone else.
75. **Everything degrades gracefully** — turn off the vector database and search still works via the normal database.
76. **Automatic retries** on transient AI provider errors, with backoff.
77. **Streaming works through proxies** — the buffering problem that usually breaks this is handled.
78. **Keep-warm ping** every 10 minutes so the free-tier server doesn't fall asleep.
79. **Fits in 512 MB of RAM** — measured at 479 MB peak.
80. **Cross-encoder reranking built and measured, but deliberately switched off** because it needs 156 MB more than the server has.
81. **Cost measured per query** — about $0.0006 for a natural-language search, $0.0011 for a full concierge turn, $0 for normal search.
82. **Model benchmark** comparing AI models on accuracy, speed and cost, with no AI judging AI.
83. **289 automated backend tests** that cost zero AI quota.
84. **16 browser tests** driving the real UI.
85. **Continuous integration** running lint, tests and a Docker build on every push.
86. **End-to-end production check** — 41 assertions against the live site, verifying real content rather than just "the server responded".
87. **Runs locally with one command** via Docker Compose.
88. **Deployed and live** — backend, frontend, database and vector store all hosted.

---

## Honest notes

Worth saying out loud, because they are scope decisions rather than gaps:

- **There is no sign-in.** "Remembering you" means remembering *this browser*. Clear your
  browser storage and the memory is gone.
- **Three things are local-only**: the weather tool, the ingestion pipeline and the reranker.
  Each for a stated reason — see [README.md](./README.md) and
  [DEMO.md](./DEMO.md#32-inbound-the-platform-consumes-an-mcp-server-local-only).
- **1,286 of 50,000 summaries are AI-written.** The rest are quoted review extracts, and are
  labelled as such rather than dressed up as AI.
- **Per-review star ratings do not exist** in the source data, so the review `min_score`
  filter is wired correctly but always returns nothing.
- **Availability is generated, not real** — the source calendar exports are too large to ship
  and one city's carries no prices.
- **No OCR.** It was the planned feature that got cut deliberately, not one that failed.
