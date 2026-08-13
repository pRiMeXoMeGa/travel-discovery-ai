# Frontend (Next.js)

A single-page booking-style product surface with a conversational concierge on top. Next.js 14 (App Router), TypeScript, Tailwind, MapLibre.

Status: implemented and verified (Phases 4 and 5), plus the v2 memory panel on the
`v2-agentic` branch. Full booking surface, NL search bar, and streaming concierge. The production build passes, and I verified it against the live backend on `:8000` (see [backend/README.md](../backend/README.md) for the contract).

## Layout

```
app/
├── layout.tsx          # root layout, Inter font, Providers
├── providers.tsx       # wishlist / compare / hover contexts + global ConciergePanel
├── page.tsx            # results: NL search bar + filters + chips + list + map
├── listings/[id]/      # property detail (gallery, calendar, reviews, price breakdown, reserve)
├── compare/            # side-by-side compare (2-4)
└── wishlist/           # saved listings
components/
├── filters/            # FilterPanel, SearchBar (dates/guests), FilterChips
├── listings/           # ListingCard, ResultsList
├── map/                # MapView (price markers, clustering, list/map sync), MiniMap
├── compare/            # CompareBar
├── concierge/          # NlSearchBar, ConciergePanel (streaming chat + memory panel)
└── ui/                 # StarRating, AmenityBadge
lib/
├── api.ts              # typed REST client (search, listing, reviews, compare, nlSearch)
├── concierge.ts        # streamConcierge() + parseSseFrames(): SSE-over-POST
├── planner.ts          # WS3 planner client: stream + resume (interrupt/resume)
├── identity.ts         # localStorage traveller/trip UUID (v2 memory)
├── search-state.ts     # filters <-> URL query-string
└── wishlist.ts         # localStorage wishlist + compare
```

## Cities and types (real Inside Airbnb data)

- Cities: Amsterdam, Lisbon, Los Angeles (the city selector and the map centroids).
- Property types use the real Airbnb `room_type` strings verbatim: `Entire home/apt`, `Private room`, `Shared room`, `Hotel room`. The filter options and `PROPERTY_TYPE_LABELS` in `lib/search-state.ts` have to match what `listings.type` stores.
- The amenities filter uses the same 18-term vocabulary the ingestion normalizes to.

## Booking surface (what's built)

- Filters: date range (availability-aware), guests, price slider, rating, property type, amenities, and sort, with removable active-filter chips (including a city chip).
- Results: listing cards (photo, price/night, total-for-stay, rating, amenities, distance) plus a MapLibre map with price markers, clustering, and hover/pan sync between map and list. The map fits its bounds to the result pins rather than a static centroid, the clusters recompute on zoom, and clicking a pin opens a popup while clicking a cluster zooms in to split it.
- Detail page: gallery, amenities grid, embedded map, reviews (filter by language and score, plus aspect scores and an AI summary — the API and client also support a `topic` filter, but no UI control is wired for it yet), availability calendar, price breakdown, and a mocked Reserve that leads to a confirmation.
- Wishlist plus compare (2-4) with an AI verdict. The backend builds the verdict from parallel per-listing review synthesis and a grounded LLM call, and the matrix still renders if the verdict isn't available.
- NL search bar (Phase 5): calls `/api/nl-search`, applies the parsed filters, and shows "understood" chips so you can see what it picked up.
- Concierge (Phase 5): mounted globally so it's reachable from any page. It streams the visible agent steps and a grounded answer, and listing citations click through to the detail page.
- Memory panel (v2): a collapsible section at the top of the concierge slide-over showing which remembered preferences were used, which were learned this turn, and — importantly — which are **enforced as hard filters** versus which are only soft preferences. Each row has a forget button. See "Memory panel" below.
- Trip planner (v2): an **Ask / Plan a trip** toggle. In Plan mode the request goes to the LangGraph planner, which can *stop and ask* — it presents the itinerary and waits for Approve or Adjust before committing. See "Planner" below.

## Natural-language search: a design note

I went with parse-on-submit (Enter, or the "AI Search" button). Pressing Enter sends the typed query to `/api/nl-search`, the LLM parses it into structured filters, and those get applied right away and shown as removable filter chips (city, dates, price, amenities, type) so you see exactly what was understood. The non-filterable bits (vibe, "near restaurants") show up as a subtle "Understood: …" note instead of chips.

Why not parse on every keystroke ("live as you type")? Parsing is an LLM call. One call per keystroke means high latency, and on a free-tier LLM with strict rate limits (which this project did hit during ingestion) it would burn through quota fast and feel laggy. Parse-on-submit is one call per intent, which is responsive, cheap, and reliable.

The option I didn't build is a debounced auto-parse (fire ~700-900 ms after the user stops typing), which feels more live while keeping the call volume bounded. It's reasonable with a paid LLM tier or generous quota plus client-side caching of identical queries, but I deferred it to protect the free-tier quota and keep the UX predictable. Switching to it would be a local change in `NlSearchBar.tsx` (just debounce the existing `nlSearch` call).

## Run

The root `docker compose up --build` starts it in dev mode. Standalone:

```bash
npm install
cp .env.local.example .env.local   # set NEXT_PUBLIC_API_URL
npm run dev                        # http://localhost:3000
```

Scripts: `dev`, `build`, `start`, `lint`, `test:e2e`.

## End-to-end tests (Playwright)

`e2e/` holds the browser suite; `playwright.config.ts` points it at `http://localhost:3000`
(override with `E2E_BASE_URL`). There's deliberately no `webServer` block — the tests assert
against the **real restored corpus**, so the compose stack has to be up first:

```bash
docker compose up -d            # from the repo root
cd frontend && npm run test:e2e
npm run test:e2e -- --grep-invert @llm   # skip the specs that spend LLM quota
```

**Run these from the host, not from the `frontend` container.** The container is
`node:20-alpine` and the Dockerfile installs no browsers, so `npx playwright test` inside it
fails with `Executable doesn't exist` — and `npx playwright install` there does not fix it
either: Playwright's Chromium is glibc-linked, so on musl it downloads happily and then dies
with `spawn … ENOENT`. Install browsers once on the host with `npx playwright install
chromium`.

Two more things that produce failures which look like product bugs but are not:

- **Warm the routes first.** The image runs `npm run dev`, so the first request to each route
  compiles it on demand (~30s for listing detail) — longer than some assertions allow. `curl`
  `/`, `/?city=<each city>`, `/wishlist` and one `/listings/{id}` before starting.
- **Don't run the suite while a heavy job shares the backend container.** Anything running
  fastembed there (for example `scripts/backfill_summaries.py`) starves the API, and the
  broadest queries time out first while narrow filtered ones still pass — which reads as a
  targeted regression rather than load.

Two files, split by cost:

- `booking-surface.spec.ts` — search cards, city switching, the price cap, filter chips, the
  MapLibre canvas, listing detail, and wishlist round-trip. No LLM calls, ~1 min.
- `concierge.spec.ts` — NL search parsing, the streaming concierge (agent step trail,
  grounded answer, itinerary cards) and the v2 memory panel. Tagged `@llm`: each test
  spends free-tier Gemini quota.
- `planner.spec.ts` — the WS3 human-in-the-loop flow: the run suspends for approval,
  approve finalises it, adjust replans and comes back for a second decision, and a control
  asserting **Ask mode is unaffected** by any of it. Tagged `@llm`.

15 tests total.

On a **fresh container**, warm the routes first (`/`, `/wishlist`, `/listings/{id}`,
`/compare`). `next dev` compiles per route on first visit — the listing detail route was
measured at 31s — and the suite otherwise reports compile latency as test failures.

Assertions target *shape and behaviour* (a price renders, every card obeys the cap, the saved
listing is the one that comes back) rather than specific listing names, so a re-ingest doesn't
break the suite. `workers: 1` is intentional — parallel agent runs hit the free-tier rate limit
and the 429s look like product failures.

Two selector notes worth knowing before adding tests: a results card is an `<article>`
wrapping **two** links (photo and content), so a bare `a[href^="/listings/"]` matches half a
card — the photo link alone contains only the room-type badge. And the step trail renders the
friendly labels in `ConciergePanel`'s `STEP_LABEL` ("Understanding your request"), not the raw
agent names.

## Config

- `NEXT_PUBLIC_API_URL` is the backend base URL (`http://localhost:8000` locally, the Render URL in the Vercel project env).

## Notes

- SSE over POST. `lib/concierge.ts` uses `fetch` plus a `ReadableStream` rather than `EventSource`, because the concierge endpoint is a POST. It parses the `data:` frames (normalizing `\r\n` to `\n`, since uvicorn sends CRLF separators) and yields typed `ConciergeEvent`s, including the structured `itinerary` event that renders as day-by-day cards with one-click swap-out.
- MapLibre CSS (`import "maplibre-gl/dist/maplibre-gl.css"`) is required or the markers won't position, and the pins collapse invisibly without it. Pin clicks call `stopPropagation()` so the map's own click handler doesn't immediately close the popup.
- The included `Dockerfile` is dev-oriented (`next dev`) for the local stack. Vercel deploys straight from git; for a production container you'd switch to a multi-stage `next build` + `next start`.

## Memory panel (v2)

The concierge remembers the traveller between sessions. The panel exists because an
invisible memory layer is indistinguishable from no memory layer — and worse, personalised
results the user cannot see read as results being *wrong*.

- **`lib/identity.ts`** mints and stores a traveller UUID (and an optional trip id) in
  `localStorage`, sent on every concierge request. This is same-browser persistence, **not
  authentication** — clear site data or switch device and you are a new traveller. It
  degrades to an anonymous turn when storage is unavailable: SSR/prerender, Safari private
  mode and "block all cookies" all throw or return null rather than breaking the turn.
- **Badges distinguish guarantees from hints.** A validated dealbreaker renders as
  `never · Shared room` / `always · elevator` because it is a real Qdrant payload filter.
  Anything the backend could not map to a payload field renders under an explicit
  "can't be enforced as a filter" heading. Telling someone a rule is enforced when it
  isn't is worse than not having the feature.
- **Forget is optimistic** — the row hides immediately and is restored if
  `DELETE /api/memory/{id}` fails, because the point of the button is to prove the memory
  is real state you control.

Two implementation notes for anyone extending it:

- Memory rides the **existing** SSE `step` event with `agent === "memory"`; there is no new
  event type. It is skipped in the step trail the way `router` is, because recall and write
  both arrive under the same agent name and the trail's `findIndex(s => s.agent === ...)`
  lookup would make the write overwrite the recall.
- `ConciergeEvent`'s `step` variant types `data` as `unknown`. A dedicated union arm for
  `agent: "memory"` does **not** narrow — the general arm types `agent` as `string`, which
  subsumes the literal, so TypeScript intersects the two `data` types down to `{}`. Use the
  `isMemoryStepData` type guard in `lib/concierge.ts`, which also validates the untrusted
  wire frame in the same place.

## Linting — known gap

`npm run lint` is defined but ESLint has never been configured, so it drops into an
interactive setup prompt. CI lints the backend with ruff only, so **the frontend currently
has no lint coverage.** Typechecking (`npx tsc --noEmit`) and the Playwright suite do run.

## Planner (WS3)

`lib/planner.ts` is a **separate client** from `streamConcierge`, on purpose. The concierge
assumes one request per turn — open a stream, consume it, done — and the planner breaks
that structurally: it can suspend at a human checkpoint and be continued later by a second
request, possibly against a different server process. Overloading the concierge client with
that would have put a working, e2e-tested path at risk for no gain.

The SSE *frame parsing* is shared (`parseSseFrames`, exported from `lib/concierge.ts`),
because reimplementing it is how you lose the CR-strip that makes uvicorn's CRLF frames
parse at all.

- **Mode is explicit, not auto-detected.** A classifier silently routing a query to the
  planner would make the interrupt look like a bug when it fires.
- **`awaiting_input` is its own event type**, not a flag on `done`. `done` means the turn
  is over and the client tears down its stream state; an interrupted run is suspended and
  the client must hold `thread_id` to continue it.
- The review card renders `within_budget` as **three** states — within / over / unknown —
  because an empty plan reports `null`, and showing a green "within budget" badge over zero
  stays is exactly the bug the backend fix removed.
