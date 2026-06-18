"use client";

import { useEffect, useRef } from "react";

interface MiniMapProps {
  lat: number;
  lng: number;
  name: string;
}

export default function MiniMap({ lat, lng, name }: MiniMapProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<unknown>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    let cancelled = false;

    import("maplibre-gl").then((mod) => {
      if (cancelled || !containerRef.current) return;
      const maplibregl = mod.default as unknown as typeof import("maplibre-gl");

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
          layers: [{ id: "carto-light", type: "raster", source: "carto-light" }],
        },
        center: [lng, lat],
        zoom: 14,
        interactive: false,
      });

      mapRef.current = map;

      map.on("load", () => {
        if (cancelled) return;
        const el = document.createElement("div");
        el.style.cssText = `
          width: 36px; height: 36px;
          background: #e61e4d;
          border: 3px solid white;
          border-radius: 50%;
          box-shadow: 0 2px 8px rgba(230,30,77,0.4);
          cursor: default;
        `;
        el.title = name;
        new maplibregl.Marker({ element: el, anchor: "center" })
          .setLngLat([lng, lat])
          .addTo(map);
      });
    });

    return () => {
      cancelled = true;
      if (mapRef.current) {
        (mapRef.current as { remove: () => void }).remove();
        mapRef.current = null;
      }
    };
  }, [lat, lng, name]);

  return <div ref={containerRef} className="w-full h-full" />;
}
