"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { search, SearchFilters, SearchResponse } from "@/lib/api";
import { FilterPanel } from "@/components/filters/FilterPanel";
import { SearchBar } from "@/components/filters/SearchBar";
import { FilterChips } from "@/components/filters/FilterChips";
import { NlSearchBar } from "@/components/concierge/NlSearchBar";
import { ResultsList } from "@/components/listings/ResultsList";
import { MapView } from "@/components/map/MapView";
import { CompareBar } from "@/components/compare/CompareBar";
import {
  DEFAULT_FILTERS,
  searchParamsToFilters,
  filtersToSearchParams,
} from "@/lib/search-state";
import { Suspense } from "react";
import Link from "next/link";
import { useWishlist } from "./providers";

function SearchPageInner() {
  const router = useRouter();
  const params = useSearchParams();
  const { wishlist } = useWishlist();

  const [filters, setFilters] = useState<SearchFilters>(() => {
    const fromUrl = searchParamsToFilters(params);
    return { ...DEFAULT_FILTERS, ...fromUrl };
  });

  const [data, setData] = useState<SearchResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const abortRef = useRef<AbortController | null>(null);

  // Sync URL when filters change
  const updateFilters = useCallback(
    (updated: Partial<SearchFilters>) => {
      setFilters((prev) => ({ ...prev, ...updated }));
    },
    []
  );

  // Push filter state to URL (separate effect to avoid setState-during-render)
  useEffect(() => {
    const sp = filtersToSearchParams(filters);
    router.replace(`/?${sp.toString()}`, { scroll: false });
  }, [filters, router]);

  // Fetch results
  useEffect(() => {
    if (abortRef.current) abortRef.current.abort();
    abortRef.current = new AbortController();

    setLoading(true);
    setError(null);

    const payload: SearchFilters = { ...filters, page_size: 20 };

    search(payload)
      .then((res) => {
        setData(res);
        setLoading(false);
      })
      .catch((err) => {
        if (err.name === "AbortError") return;
        setError(err.message ?? "Failed to load listings");
        setLoading(false);
      });
  }, [filters]);

  const handleRemoveFilter = useCallback(
    (key: keyof SearchFilters | { amenity?: string; pt?: string } | "all") => {
      if (key === "all") {
        updateFilters({
          price_min: undefined,
          price_max: undefined,
          min_rating: undefined,
          property_types: [],
          amenities: [],
          sort: "popularity",
          check_in: undefined,
          check_out: undefined,
          page: 1,
        });
        return;
      }
      if (typeof key === "object") {
        if (key.amenity) {
          updateFilters({
            amenities: (filters.amenities ?? []).filter((a) => a !== key.amenity),
            page: 1,
          });
        }
        if (key.pt) {
          updateFilters({
            property_types: (filters.property_types ?? []).filter((t) => t !== key.pt),
            page: 1,
          });
        }
        return;
      }
      if (key === "check_in") {
        updateFilters({ check_in: undefined, check_out: undefined, page: 1 });
        return;
      }
      if (key === "price_min") {
        updateFilters({ price_min: undefined, price_max: undefined, page: 1 });
        return;
      }
      updateFilters({ [key]: undefined, page: 1 } as Partial<SearchFilters>);
    },
    [filters, updateFilters]
  );

  const handleViewportSearch = useCallback(
    (lat: number, lng: number) => {
      updateFilters({ near_lat: lat, near_lng: lng, sort: "distance", page: 1 });
    },
    [updateFilters]
  );

  return (
    <div className="flex flex-col h-screen overflow-hidden bg-gray-50">
      {/* Top Header */}
      <header className="bg-white border-b border-gray-100 px-4 py-3 flex-shrink-0 z-30">
        <div className="flex items-center gap-4">
          {/* Logo */}
          <Link href="/" className="flex items-center gap-2 flex-shrink-0">
            <div className="w-8 h-8 bg-[#e61e4d] rounded-xl flex items-center justify-center">
              <svg className="w-4 h-4 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3.055 11H5a2 2 0 012 2v1a2 2 0 002 2 2 2 0 012 2v2.945M8 3.935V5.5A2.5 2.5 0 0010.5 8h.5a2 2 0 012 2 2 2 0 104 0 2 2 0 012-2h1.064M15 20.488V18a2 2 0 012-2h3.064" />
              </svg>
            </div>
            <span className="text-lg font-bold text-gray-900 hidden sm:block">
              Travel<span className="text-[#e61e4d]">AI</span>
            </span>
          </Link>

          {/* Search bar */}
          <div className="flex-1">
            <SearchBar filters={filters} onChange={updateFilters} />
          </div>

          {/* Wishlist link */}
          <Link
            href="/wishlist"
            className="flex items-center gap-1.5 px-3 py-2 rounded-xl border border-gray-200 text-sm font-medium text-gray-600 hover:border-gray-400 hover:bg-gray-50 transition-colors flex-shrink-0"
          >
            <svg className="w-4 h-4 text-[#e61e4d]" viewBox="0 0 24 24" fill="currentColor">
              <path d="M21 8.25c0-2.485-2.099-4.5-4.688-4.5-1.935 0-3.597 1.126-4.312 2.733-.715-1.607-2.377-2.733-4.313-2.733C5.1 3.75 3 5.765 3 8.25c0 7.22 9 12 9 12s9-4.78 9-12z" />
            </svg>
            Saved {wishlist.length > 0 && <span className="text-xs bg-[#e61e4d] text-white rounded-full w-4 h-4 flex items-center justify-center">{wishlist.length}</span>}
          </Link>

          {/* Sidebar toggle */}
          <button
            onClick={() => setSidebarOpen((v) => !v)}
            className="flex items-center gap-1.5 px-3 py-2 rounded-xl border border-gray-200 text-sm font-medium text-gray-600 hover:border-gray-400 transition-colors flex-shrink-0"
            aria-label="Toggle filters"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M3 4a1 1 0 011-1h16a1 1 0 011 1v2a1 1 0 01-.293.707L13 13.414V19a1 1 0 01-.553.894l-4 2A1 1 0 017 21v-7.586L3.293 6.707A1 1 0 013 6V4z" />
            </svg>
            Filters
          </button>
        </div>

        {/* Natural-language AI search — runs alongside the traditional filters */}
        <div className="mt-2.5">
          <NlSearchBar onApply={updateFilters} />
        </div>

        {/* Active filter chips (update to reflect what the NL query was understood as) */}
        <div className="mt-2.5">
          <FilterChips
            filters={filters}
            onRemove={handleRemoveFilter}
            total={data?.total}
          />
        </div>
      </header>

      {/* Main content */}
      <div className="flex flex-1 overflow-hidden">
        {/* Sidebar filters */}
        <aside
          className={`${sidebarOpen ? "w-64" : "w-0"} flex-shrink-0 bg-white border-r border-gray-100 overflow-hidden transition-all duration-200 flex flex-col`}
        >
          <div className="p-4 flex-1 overflow-y-auto filter-scroll">
            <FilterPanel filters={filters} onChange={updateFilters} />
          </div>
        </aside>

        {/* Results list */}
        <section className="flex-1 overflow-y-auto p-4 pb-20">
          <ResultsList
            data={data}
            loading={loading}
            error={error}
            filters={filters}
            onPageChange={(page) => updateFilters({ page })}
          />
        </section>

        {/* Map */}
        <section className="w-[42%] flex-shrink-0 border-l border-gray-100 overflow-hidden">
          <MapView
            listings={data?.results ?? []}
            filters={filters}
            onViewportSearch={handleViewportSearch}
          />
        </section>
      </div>

      {/* Compare bar (sticky bottom) */}
      <CompareBar />
    </div>
  );
}

export default function Home() {
  return (
    <Suspense>
      <SearchPageInner />
    </Suspense>
  );
}
