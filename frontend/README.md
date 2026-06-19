# Frontend — Next.js

Single-page booking-style product surface + conversational concierge. Next.js 14 (App Router), TypeScript, Tailwind, MapLibre.

> **Status:** implemented & verified (Phases 4 + 5). Full booking surface + NL search bar + streaming concierge. Production build passes; verified against the live backend on `:8000` (see [backend/README.md](../backend/README.md) for the contract).

## Layout

```
app/
├── layout.tsx          # root layout, Inter font, Providers
├── providers.tsx       # wishlist / compare / hover contexts + global ConciergePanel
├── page.tsx            # results: NL search bar + filters + chips + list + map
├── listings/[id]/      # property detail (gallery, calendar, reviews, price breakdown, reserve)
├── compare/            # side-by-side compare (2–4)
└── wishlist/           # saved listings
components/
├── filters/            # FilterPanel, SearchBar (dates/guests), FilterChips
├── listings/           # ListingCard, ResultsList
├── map/                # MapView (price markers, clustering, list↔map sync), MiniMap
├── compare/            # CompareBar
├── concierge/          # NlSearchBar, ConciergePanel (streaming chat)   ← Phase 5
└── ui/                 # StarRating, AmenityBadge
lib/
├── api.ts              # typed REST client (search, listing, reviews, compare, nlSearch)
├── concierge.ts        # streamConcierge(): SSE-over-POST → step/data/token/done events
├── search-state.ts     # filters ↔ URL query-string
└── wishlist.ts         # localStorage wishlist + compare
```

## Cities & types (real Inside Airbnb data)

- **Cities:** Amsterdam, Lisbon, Los Angeles (the city selector + map centroids).
- **Property types** use the real Airbnb `room_type` strings verbatim: `Entire home/apt`, `Private room`, `Shared room`, `Hotel room` (the filter options + `lib/search-state.ts` `PROPERTY_TYPE_LABELS` must match what `listings.type` stores).
- Amenities filter uses the 18-term canonical vocabulary the ingestion normalizes to.

## Booking surface (implemented)

- ✅ **Filters:** date range (availability-aware), guests, price slider, rating, property type, amenities, sort — with removable active-filter chips (incl. a **city** chip).
- ✅ **Results:** listing cards (photo, price/night, total-for-stay, rating, amenities, distance) + **MapLibre** map with price markers, clustering, and **map↔list hover/pan sync**. The map **fits its bounds to the result pins** (not a static centroid), clusters re-compute on zoom, and clicking a pin opens a popup / a cluster zooms in to split.
- ✅ **Detail page:** gallery, amenities grid, embedded map, reviews (filter by language/score/topic + aspect scores + AI summary), availability calendar, price breakdown, mocked Reserve → confirmation.
- ✅ **Wishlist** + **compare (2–4)** with an AI verdict (the backend builds it from parallel per-listing review synthesis + a grounded LLM call; the matrix still renders if the verdict is unavailable).
- ✅ **NL search bar** (Phase 5) — calls `/api/nl-search`, applies the parsed filters, and shows "understood" chips so the user sees what was parsed.
- ✅ **Concierge** (Phase 5) — mounted globally (reachable from any page); streams visible agent steps + a grounded answer; listing citations click through to the detail page.

## Natural-language search — design note (trade-off)

**Chosen: parse-on-submit (Enter / "AI Search").** Pressing Enter sends the typed query to `/api/nl-search`, which the LLM parses into structured filters; those are applied immediately and shown as **removable filter chips** (city, dates, price, amenities, type), so the user sees exactly what was understood. Non-filterable bits (vibe, "near restaurants") surface as a subtle "Understood: …" note rather than chips.

**Why not parse on every keystroke ("live as you type"):** NL parsing is an LLM call. Firing one per keystroke means high latency, and — on a free-tier LLM with strict rate/quota limits (which this project hit during ingestion) — it would exhaust quota fast and feel laggy. Parse-on-submit is one call per intent: responsive, cheap, and reliable.

**Alternate option (not implemented):** a **debounced auto-parse** (~700–900 ms after the user stops typing) gives a more "live" feel while bounding call volume. It's viable with a paid LLM tier or generous quota, plus client-side caching of identical queries; deferred here to protect free-tier quota and keep the UX predictable. Switching to it is localized to `NlSearchBar.tsx` (debounce the existing `nlSearch` call).

## Run

Started by the root `docker compose up --build` (dev mode). Standalone:

```bash
npm install
cp .env.local.example .env.local   # set NEXT_PUBLIC_API_URL
npm run dev                        # http://localhost:3000
```

Scripts: `dev`, `build`, `start`, `lint`.

## Config

- `NEXT_PUBLIC_API_URL` — backend base URL (`http://localhost:8000` locally; the Render URL in Vercel project env).

## Notes

- **SSE over POST** (`lib/concierge.ts`) uses `fetch` + `ReadableStream`, not `EventSource`, because the concierge endpoint is a POST. Parses `data:` frames (normalizing `\r\n` → `\n` — uvicorn sends CRLF separators) and yields typed `ConciergeEvent`s, including the structured `itinerary` event rendered as day-by-day cards with one-click swap-out.
- **MapLibre CSS** (`import "maplibre-gl/dist/maplibre-gl.css"`) is required for markers to position — without it pins collapse invisibly. Pin clicks `stopPropagation()` so the map's click handler doesn't immediately close the popup.
- The included `Dockerfile` is **dev-oriented** (`next dev`) for the local stack. Vercel deploys from git directly; for a production container, switch to a multi-stage `next build` + `next start`.
