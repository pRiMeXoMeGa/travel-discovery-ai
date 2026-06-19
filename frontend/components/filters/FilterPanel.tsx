"use client";

import { useState, useCallback } from "react";
import { SearchFilters } from "@/lib/api";
import {
  AMENITY_LABELS,
  PROPERTY_TYPE_LABELS,
  SORT_LABELS,
  CITIES,
} from "@/lib/search-state";
import { currencySymbol } from "@/lib/currency";

interface FilterPanelProps {
  filters: SearchFilters;
  onChange: (updated: Partial<SearchFilters>) => void;
}

const AMENITY_KEYS = Object.keys(AMENITY_LABELS);
const PROPERTY_TYPES = Object.keys(PROPERTY_TYPE_LABELS);

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="py-4 border-b border-gray-100 last:border-0">
      <h3 className="text-xs font-semibold uppercase tracking-wider text-gray-400 mb-3">
        {title}
      </h3>
      {children}
    </div>
  );
}

export function FilterPanel({ filters, onChange }: FilterPanelProps) {
  const [priceMin, setPriceMin] = useState(filters.price_min ?? 0);
  const [priceMax, setPriceMax] = useState(filters.price_max ?? 800);

  const commitPrice = useCallback(() => {
    onChange({ price_min: priceMin, price_max: priceMax, page: 1 });
  }, [priceMin, priceMax, onChange]);

  const togglePropertyType = (type: string) => {
    const current = filters.property_types ?? [];
    const next = current.includes(type)
      ? current.filter((t) => t !== type)
      : [...current, type];
    onChange({ property_types: next, page: 1 });
  };

  const toggleAmenity = (am: string) => {
    const current = filters.amenities ?? [];
    const next = current.includes(am)
      ? current.filter((a) => a !== am)
      : [...current, am];
    onChange({ amenities: next, page: 1 });
  };

  return (
    <div className="filter-scroll overflow-y-auto h-full pb-8">
      {/* City */}
      <Section title="Destination">
        <div className="flex flex-col gap-1.5">
          {CITIES.map((city) => {
            const flag =
              city === "Amsterdam" ? "🇳🇱"
              : city === "Lisbon" ? "🇵🇹"
              : "🇺🇸";
            return (
              <button
                key={city}
                onClick={() => onChange({ city, page: 1 })}
                className={`text-left px-3 py-2 rounded-lg text-sm font-medium transition-colors ${
                  filters.city === city
                    ? "bg-gray-900 text-white"
                    : "text-gray-700 hover:bg-gray-100"
                }`}
              >
                {flag} {city}
              </button>
            );
          })}
        </div>
      </Section>

      {/* Sort */}
      <Section title="Sort by">
        <div className="flex flex-col gap-1">
          {Object.entries(SORT_LABELS).map(([val, label]) => (
            <button
              key={val}
              onClick={() =>
                onChange({ sort: val as SearchFilters["sort"], page: 1 })
              }
              className={`text-left px-3 py-1.5 rounded-lg text-sm transition-colors ${
                filters.sort === val
                  ? "text-[#e61e4d] font-semibold bg-[#fff0f3]"
                  : "text-gray-600 hover:bg-gray-50"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
      </Section>

      {/* Price range */}
      <Section title="Price per night">
        <div className="px-1">
          <div className="flex justify-between text-sm font-semibold text-gray-800 mb-3">
            <span>{currencySymbol(filters.city)}{priceMin}</span>
            <span>{currencySymbol(filters.city)}{priceMax}+</span>
          </div>
          <div className="relative h-6 flex items-center mb-1">
            <div
              className="absolute h-1 bg-gray-200 rounded-full"
              style={{ left: 0, right: 0 }}
            />
            <div
              className="absolute h-1 bg-gray-900 rounded-full"
              style={{
                left: `${(priceMin / 800) * 100}%`,
                right: `${100 - (priceMax / 800) * 100}%`,
              }}
            />
            <input
              type="range"
              min={0}
              max={800}
              step={10}
              value={priceMin}
              className="absolute w-full pointer-events-none accent-gray-900"
              style={{ background: "transparent" }}
              onChange={(e) => setPriceMin(Math.min(Number(e.target.value), priceMax - 10))}
              onMouseUp={commitPrice}
              onTouchEnd={commitPrice}
            />
            <input
              type="range"
              min={0}
              max={800}
              step={10}
              value={priceMax}
              className="absolute w-full pointer-events-none accent-gray-900"
              style={{ background: "transparent" }}
              onChange={(e) => setPriceMax(Math.max(Number(e.target.value), priceMin + 10))}
              onMouseUp={commitPrice}
              onTouchEnd={commitPrice}
            />
          </div>
          <p className="text-xs text-gray-400 mt-1 text-center">
            Slide to set range
          </p>
        </div>
      </Section>

      {/* Min rating */}
      <Section title="Minimum rating">
        <div className="flex gap-2">
          {[null, 3, 3.5, 4, 4.5].map((val) => (
            <button
              key={val ?? "any"}
              onClick={() => onChange({ min_rating: val ?? undefined, page: 1 })}
              className={`flex-1 py-1.5 rounded-lg text-xs font-semibold border transition-colors ${
                (filters.min_rating ?? null) === val
                  ? "bg-gray-900 text-white border-gray-900"
                  : "border-gray-200 text-gray-600 hover:border-gray-400"
              }`}
            >
              {val == null ? "Any" : `${val}+`}
            </button>
          ))}
        </div>
      </Section>

      {/* Property types */}
      <Section title="Property type">
        <div className="grid grid-cols-2 gap-2">
          {PROPERTY_TYPES.map((type) => {
            const selected = (filters.property_types ?? []).includes(type);
            return (
              <button
                key={type}
                onClick={() => togglePropertyType(type)}
                className={`px-3 py-2 rounded-xl text-xs font-medium border transition-all ${
                  selected
                    ? "bg-gray-900 text-white border-gray-900"
                    : "border-gray-200 text-gray-600 hover:border-gray-400 hover:bg-gray-50"
                }`}
              >
                {PROPERTY_TYPE_LABELS[type]}
              </button>
            );
          })}
        </div>
      </Section>

      {/* Amenities */}
      <Section title="Amenities">
        <div className="flex flex-col gap-2">
          {AMENITY_KEYS.map((am) => {
            const selected = (filters.amenities ?? []).includes(am);
            return (
              <label
                key={am}
                className="flex items-center gap-2 cursor-pointer group"
              >
                <span
                  className={`w-4 h-4 rounded border flex-shrink-0 flex items-center justify-center transition-colors ${
                    selected
                      ? "bg-gray-900 border-gray-900"
                      : "border-gray-300 group-hover:border-gray-500"
                  }`}
                  onClick={() => toggleAmenity(am)}
                >
                  {selected && (
                    <svg
                      className="w-2.5 h-2.5 text-white"
                      fill="none"
                      viewBox="0 0 24 24"
                      stroke="currentColor"
                      strokeWidth={3}
                    >
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        d="M5 13l4 4L19 7"
                      />
                    </svg>
                  )}
                </span>
                <span
                  className="text-sm text-gray-700"
                  onClick={() => toggleAmenity(am)}
                >
                  {AMENITY_LABELS[am]}
                </span>
              </label>
            );
          })}
        </div>
      </Section>
    </div>
  );
}
