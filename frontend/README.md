# Frontend (Next.js)

A single-page booking-style product surface with a conversational concierge on top. Next.js 14 (App Router), TypeScript, Tailwind, MapLibre.

Status: implemented and verified (Phases 4 and 5). Full booking surface, NL search bar, and streaming concierge. The production build passes, and I verified it against the live backend on `:8000` (see [backend/README.md](../backend/README.md) for the contract).

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
├── concierge/          # NlSearchBar, ConciergePanel (streaming chat)   <- Phase 5
└── ui/                 # StarRating, AmenityBadge
lib/
├── api.ts              # typed REST client (search, listing, reviews, compare, nlSearch)
├── concierge.ts        # streamConcierge(): SSE-over-POST -> step/data/token/done events
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

Two files, split by cost:

- `booking-surface.spec.ts` — search cards, city switching, the price cap, filter chips, the
  MapLibre canvas, listing detail, and wishlist round-trip. No LLM calls, ~1 min.
- `concierge.spec.ts` — NL search parsing and the streaming concierge (agent step trail,
  grounded answer, itinerary cards). Tagged `@llm`: each test spends free-tier Gemini quota.

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
