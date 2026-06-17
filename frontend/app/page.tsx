// Results page — the core booking surface (brief §2.2) + NL search bar (§2.3).
// Skeleton: lays out the regions; each is a TODO component.
export default function Home() {
  return (
    <main className="min-h-screen">
      {/* Top bar: NL search bar runs ALONGSIDE traditional filters. */}
      <header className="border-b p-4">
        <h1 className="text-xl font-semibold">Travel Discovery AI</h1>
        {/* TODO: <NlSearchBar /> — parses NL -> filters, shows understood chips */}
        {/* TODO: filter chips reflecting what the NL query was understood as */}
      </header>

      <div className="flex">
        {/* Left: filters (date range, guests, price, rating, type, amenities, sort) */}
        <aside className="w-72 border-r p-4">
          {/* TODO: <Filters /> */}
          <p className="text-sm text-gray-500">Filters (TODO)</p>
        </aside>

        {/* Middle: results list with proper listing cards */}
        <section className="flex-1 p-4">
          {/* TODO: <ResultsList /> — cards: photo, name, price/night, total, rating,
              amenities, distance; pagination or infinite scroll */}
          <p className="text-sm text-gray-500">Results (TODO)</p>
        </section>

        {/* Right: map view, synced with the list on hover/pan */}
        <section className="w-[40%] border-l">
          {/* TODO: <MapView /> — MapLibre, price markers, clustering, list<->map sync */}
          <div className="h-full min-h-[400px] grid place-items-center text-gray-400">
            Map (TODO)
          </div>
        </section>
      </div>

      {/* Concierge: accessible from anywhere, streams visible agent steps. */}
      {/* TODO: <ConciergeWidget /> using lib/concierge.ts */}
    </main>
  );
}
