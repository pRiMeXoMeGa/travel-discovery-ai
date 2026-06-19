"use client";

// Required for MapLibre marker/popup/control positioning — without this CSS,
// markers get no `position:absolute` and collapse to the corner (invisible).
import "maplibre-gl/dist/maplibre-gl.css";
import { useEffect, useRef, useCallback, useState } from "react";
import { ListingCard, SearchFilters } from "@/lib/api";
import { price } from "@/lib/currency";
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

const AMSTERDAM_CENTER: [number, number] = [4.9041, 52.3676];
const LISBON_CENTER: [number, number] = [-9.1393, 38.7223];
const LOS_ANGELES_CENTER: [number, number] = [-118.2437, 34.0522];

function getCityCenter(city?: string): [number, number] {
  if (city === "Amsterdam") return AMSTERDAM_CENTER;
  if (city === "Los Angeles") return LOS_ANGELES_CENTER;
  if (city === "Lisbon") return LISBON_CENTER;
  // Default to Amsterdam (new primary city)
  return AMSTERDAM_CENTER;
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
  const [zoomTick, setZoomTick] = useState(0);  // bumps on zoomend → re-cluster
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

      // Re-cluster markers when the zoom changes so clusters split/merge as you
      // zoom (without this, clicking a cluster zoomed in but never broke it up).
      map.on("zoomend", () => setZoomTick((t) => t + 1));
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

  // Frame the map to the ACTUAL property coordinates whenever results change.
  // We have real lat/lng per listing, so fit the view to the pins rather than a
  // static city centroid (which left pins off-screen). Skipped during a viewport
  // "search this area" so we don't fight the user's pan.
  useEffect(() => {
    if (!mapRef.current || !mapReady || !maplibregl) return;
    // Viewport-driven search: respect the user's current pan/zoom.
    if (filters.near_lat != null && filters.near_lng != null) return;

    if (!listings.length) {
      // No results — fall back to the selected city's centroid.
      if (filters.city !== lastCityRef.current) {
        mapRef.current.flyTo({ center: getCityCenter(filters.city), zoom: 11, duration: 800 });
        lastCityRef.current = filters.city;
      }
      return;
    }

    const bounds = new maplibregl.LngLatBounds();
    listings.forEach((l) => {
      if (typeof l.lng === "number" && typeof l.lat === "number") {
        bounds.extend([l.lng, l.lat]);
      }
    });
    if (!bounds.isEmpty()) {
      mapRef.current.fitBounds(bounds, { padding: 64, maxZoom: 15, duration: 800 });
    }
    lastCityRef.current = filters.city;
  }, [listings, mapReady, filters.city, filters.near_lat, filters.near_lng]);

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
      el.addEventListener("click", (e) => {
        e.stopPropagation();
        // Zoom in enough to break the cluster apart; re-clustering runs on zoomend.
        map.flyTo({ center: [cluster.lng, cluster.lat], zoom: Math.min(zoom + 3, 16), duration: 700 });
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
      el.textContent = price(listing.price_per_night, listing.city);
      el.title = listing.name;

      el.addEventListener("click", (e) => {
        // Stop the click bubbling to the map container, whose 'click' handler
        // would otherwise immediately close the popup we're about to open.
        e.stopPropagation();
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
  }, [listings, mapReady, clusterListings, setHoveredId, zoomTick]);

  // Update marker highlighting when hoveredId changes. (Popup is NOT closed on
  // hover change — only via the × button or a map click — so it stays open long
  // enough to click through to the listing.)
  useEffect(() => {
    markersRef.current.forEach(({ el }, id) => {
      el.classList.toggle("hovered", id === hoveredId);
    });
  }, [hoveredId]);

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
            left: Math.max(8, Math.min(popup.x - 112, (containerRef.current?.offsetWidth ?? 600) - 240)),
            top: Math.max(8, popup.y - 200),
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
                  {price(popup.listing.price_per_night, popup.listing.city)}<span className="font-normal text-xs text-gray-500">/night</span>
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
