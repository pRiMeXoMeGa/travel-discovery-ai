# Frontend — Next.js

Single-page booking-style product surface + conversational concierge. Next.js 14 (App Router), TypeScript, Tailwind, MapLibre.

> **Status:** scaffold (Phase 4 — pending). Layout regions + the API/SSE clients (`lib/api.ts`, `lib/concierge.ts`) are in place; UI components are `TODO`. **The backend it consumes is live and verified** on `:8000` — `/api/search`, `/api/listings/{id}`, `/api/listings/{id}/reviews`, `/api/nl-search`, and the SSE `/api/concierge/stream` (see [backend/README.md](../backend/README.md) for the contract).

## Layout

```
app/
├── layout.tsx     # root layout + global styles
├── page.tsx       # results page: filters · list · map · concierge regions (TODO components)
└── globals.css    # Tailwind entry
lib/
├── api.ts         # typed client: search(), nlSearch() + SearchFilters/ListingCard types
└── concierge.ts   # streamConcierge(): SSE-over-POST reader yielding step/token/done events
```

## What to build (booking surface)

- **Filters:** date range (availability-aware), guests, price slider, rating, property type, amenities, sort.
- **Results:** listing cards (photo, name, price/night, total-for-stay, rating, amenities, distance) + **MapLibre** view with price markers, clustering, and **map↔list sync on hover/pan**.
- **Detail page:** gallery, amenities grid, embedded map, reviews (filter by language/score/topic + aspect scores + AI summary), availability calendar, price breakdown, mocked Reserve → confirmation.
- **Wishlist** + **compare (2–4)** with AI verdict.
- **NL search bar** at the top → updates filter chips to show what was understood.
- **Concierge** accessible anywhere, streaming visible agent steps; citations click through to listing/review.

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

- **SSE over POST** (`lib/concierge.ts`) uses `fetch` + `ReadableStream`, not `EventSource`, because the concierge endpoint is a POST. Parses `data:` frames and yields typed `ConciergeEvent`s.
- The included `Dockerfile` is **dev-oriented** (`next dev`) for the local stack. Vercel deploys from git directly; for a production container, switch to a multi-stage `next build` + `next start`.
