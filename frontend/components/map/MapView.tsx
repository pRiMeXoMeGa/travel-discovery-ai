"use client";

import { useEffect, useRef, useCallback, useState } from "react";
import { ListingCard, SearchFilters } from "@/lib/api";
import { useHover } from "@/app/providers";
import Link from "next/link";
import Image from "next/image";

// MapLibre is a browser-only library — import via dynamic or guarded require
let maplibregl: typeof import("maplibre-gl") | null = null;

interface MapViewProps {
  listings: ListingCard[];
  filters: SearchFilters;
  onViewportSearch?: (lat: number, lng: number) => void;
}

const LISBON_CENTER: [number, number] = [-9.139, 38.722];
const DUBAI_CENTER: [number, number] = [55.296, 25.276];

function getCityCenter(city?: string): [number, number] {
  if (city === "Dubai") return DUBAI_CENTER;
  return LISBON_CENTER;
}

interface PopupData {
  listing: ListingCard;
  x: number;
  y: number;
}

export function MapView({ listings, filters, onViewportSearch }: MapViewProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<import("maplibre-gl").Map | null>(null);
  const markersRef = useRef<Map<string, { marker: import("maplibre-gl").Marker; el: HTMLDivElement }>>(new Map());
  const clusterMarkersRef = useRef<import("maplibre-gl").Marker[]>([]);
  const { hoveredId, setHoveredId } = useHover();
  const [popup, setPopup] = useState<PopupData | null>(null);
  const [mapReady, setMapReady] = useState(false);
  const [moveSearchVisible, setMoveSearchVisible] = useState(false);
  const moveTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastCityRef = useRef<string | undefined>(undefined);

  // Load maplibre-gl lazily (browser only)
  useEffect(() => {
    let cancelled = false;
    import("maplibre-gl").then((mod) => {
      if (cancelled) return;
      maplibregl = mod.default as unknown as typeof import("maplibre-gl");

      if (!containerRef.current) return;

      const map = new maplibregl.Map({
        container: containerRef.current,
        style: {
          version: 8,
          sources: {
            "carto-light": {
              type: "raster",
              tiles: [
                "https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png",
              ],
              tileSize: 256,
              attribution: "© CartoDB © OpenStreetMap",
            },
          },
          layers: [
            {
              id: "carto-light",
              type: "raster",
              source: "carto-light",
            },
          ],
        },
        center: getCityCenter(filters.city),
        zoom: 12,
      });

      map.on("load", () => {
        if (cancelled) return;
        setMapReady(true);
        mapRef.current = map;
        lastCityRef.current = filters.city;
      });

      map.on("movestart", () => {
        if (moveTimeoutRef.current) clearTimeout(moveTimeoutRef.current);
        setMoveSearchVisible(false);
      });

      map.on("moveend", () => {
        moveTimeoutRef.current = setTimeout(() => {
          setMoveSearchVisible(true);
        }, 600);
      });
    });

    return () => {
      cancelled = true;
      if (mapRef.current) {
        mapRef.current.remove();
        mapRef.current = null;
      }
      setMapReady(false);
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Re-center map when city filter changes
  useEffect(() => {
    if (!mapRef.current || !mapReady) return;
    if (filters.city !== lastCityRef.current) {
      mapRef.current.flyTo({
        center: getCityCenter(filters.city),
        zoom: 12,
        duration: 1200,
      });
      lastCityRef.current = filters.city;
    }
  }, [filters.city, mapReady]);

  // Cluster listings into buckets by proximity at current zoom
  const clusterListings = useCallback((
    lsts: ListingCard[],
    zoom: number
  ): { clusters: { lat: number; lng: number; count: number; listings: ListingCard[] }[]; singles: ListingCard[] } => {
    const cellSize = zoom < 11 ? 0.1 : zoom < 13 ? 0.03 : zoom < 15 ? 0.01 : 0;
    if (cellSize === 0) return { clusters: [], singles: lsts };

    const cells = new Map<string, ListingCard[]>();
    lsts.forEach((l) => {
      const key = `${Math.floor(l.lat / cellSize)},${Math.floor(l.lng / cellSize)}`;
      if (!cells.has(key)) cells.set(key, []);
      cells.get(key)!.push(l);
    });

    const clusters: { lat: number; lng: number; count: number; listings: ListingCard[] }[] = [];
    const singles: ListingCard[] = [];

    cells.forEach((group) => {
      if (group.length > 1) {
        const lat = group.reduce((s, l) => s + l.lat, 0) / group.length;
        const lng = group.reduce((s, l) => s + l.lng, 0) / group.length;
        clusters.push({ lat, lng, count: group.length, listings: group });
      } else {
        singles.push(group[0]);
      }
    });

    return { clusters, singles };
  }, []);

  // Render markers when listings or map readiness changes
  useEffect(() => {
    if (!mapRef.current || !mapReady || !maplibregl) return;
    const map = mapRef.current;

    // Clear old markers
    markersRef.current.forEach(({ marker }) => marker.remove());
    markersRef.current.clear();
    clusterMarkersRef.current.forEach((m) => m.remove());
    clusterMarkersRef.current = [];

    const zoom = map.getZoom();
    const { clusters, singles } = clusterListings(listings, zoom);

    // Cluster markers
    clusters.forEach((cluster) => {
      const size = Math.min(44, 32 + cluster.count * 2);
      const el = document.createElement("div");
      el.className = "cluster-marker";
      el.style.width = `${size}px`;
      el.style.height = `${size}px`;
      el.textContent = String(cluster.count);
      el.title = `${cluster.count} listings`;
      el.addEventListener("click", () => {
        map.flyTo({ center: [cluster.lng, cluster.lat], zoom: zoom + 2, duration: 800 });
      });

      const m = new maplibregl!.Marker({ element: el, anchor: "center" })
        .setLngLat([cluster.lng, cluster.lat])
        .addTo(map);
      clusterMarkersRef.current.push(m);
    });

    // Individual price markers
    singles.forEach((listing) => {
      const el = document.createElement("div");
      el.className = "price-marker";
      el.textContent = `$${Math.round(listing.price_per_night)}`;
      el.title = listing.name;

      el.addEventListener("click", () => {
        const { x, y } = map.project([listing.lng, listing.lat]);
        setPopup({ listing, x, y });
        setHoveredId(listing.id);
      });

      el.addEventListener("mouseenter", () => setHoveredId(listing.id));
      el.addEventListener("mouseleave", () => {
        setHoveredId(null);
      });

      const m = new maplibregl!.Marker({ element: el, anchor: "bottom" })
        .setLngLat([listing.lng, listing.lat])
        .addTo(map);

      markersRef.current.set(listing.id, { marker: m, el });
    });
  }, [listings, mapReady, clusterListings, setHoveredId]);

  // Update marker highlighting when hoveredId changes
  useEffect(() => {
    markersRef.current.forEach(({ el }, id) => {
      if (id === hoveredId) {
        el.classList.add("hovered");
      } else {
        el.classList.remove("hovered");
      }
    });

    // Close popup when hover changes to something different
    if (popup && popup.listing.id !== hoveredId) {
      setPopup(null);
    }
  }, [hoveredId, popup]);

  // Close popup on map click
  useEffect(() => {
    if (!mapRef.current) return;
    const handler = () => setPopup(null);
    mapRef.current.on("click", handler);
    return () => { mapRef.current?.off("click", handler); };
  }, [mapReady]);

  const handleMoveSearch = useCallback(() => {
    if (!mapRef.current || !onViewportSearch) return;
    const center = mapRef.current.getCenter();
    onViewportSearch(center.lat, center.lng);
    setMoveSearchVisible(false);
  }, [onViewportSearch]);

  return (
    <div className="relative w-full h-full">
      <div ref={containerRef} className="w-full h-full" />

      {/* Search as I move button */}
      {moveSearchVisible && onViewportSearch && (
        <div className="absolute top-4 left-1/2 -translate-x-1/2 z-20">
          <button
            onClick={handleMoveSearch}
            className="px-4 py-2 bg-white rounded-full shadow-lg text-sm font-semibold text-gray-800 border border-gray-200 hover:shadow-xl transition-all flex items-center gap-2"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
            </svg>
            Search this area
          </button>
        </div>
      )}

      {/* Map popup card */}
      {popup && (
        <div
          className="absolute z-30 w-56 bg-white rounded-2xl shadow-2xl overflow-hidden border border-gray-100"
          style={{
            left: Math.min(popup.x - 112, (containerRef.current?.offsetWidth ?? 600) - 240),
            top: popup.y - 200,
          }}
        >
          <button
            onClick={() => setPopup(null)}
            className="absolute top-2 right-2 z-10 w-6 h-6 bg-white/90 rounded-full flex items-center justify-center text-gray-500 hover:text-gray-800 text-sm"
          >
            ×
          </button>
          <Link href={`/listings/${popup.listing.id}`} className="block">
            <div className="relative h-32 bg-gray-100">
              {popup.listing.photo && (
                <Image
                  src={popup.listing.photo}
                  alt={popup.listing.name}
                  fill
                  className="object-cover"
                  sizes="224px"
                />
              )}
            </div>
            <div className="p-3">
              <p className="text-xs text-gray-400 mb-0.5">
                {popup.listing.neighbourhood ?? popup.listing.city}
              </p>
              <p className="text-sm font-semibold text-gray-900 leading-tight mb-1">
                {popup.listing.name}
              </p>
              <div className="flex items-center justify-between">
                <span className="text-sm font-bold text-gray-900">
                  ${Math.round(popup.listing.price_per_night)}<span className="font-normal text-xs text-gray-500">/night</span>
                </span>
                {popup.listing.rating != null && (
                  <span className="text-xs text-gray-600">★ {popup.listing.rating.toFixed(1)}</span>
                )}
              </div>
            </div>
          </Link>
        </div>
      )}

      {/* Loading overlay */}
      {!mapReady && (
        <div className="absolute inset-0 bg-gray-100 flex items-center justify-center">
          <div className="text-center">
            <div className="w-8 h-8 border-2 border-gray-300 border-t-gray-700 rounded-full animate-spin mx-auto mb-2" />
            <p className="text-xs text-gray-400">Loading map...</p>
          </div>
        </div>
      )}
    </div>
  );
}
