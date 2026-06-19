"use client";

import { SearchFilters } from "@/lib/api";
import {
  AMENITY_LABELS,
  PROPERTY_TYPE_LABELS,
  SORT_LABELS,
} from "@/lib/search-state";
import { format, parseISO } from "date-fns";
import { currencySymbol } from "@/lib/currency";

interface FilterChipsProps {
  filters: SearchFilters;
  onRemove: (key: keyof SearchFilters | { amenity?: string; pt?: string }) => void;
  total?: number;
}

interface Chip {
  id: string;
  label: string;
  onRemove: () => void;
  highlight?: boolean;
}

export function FilterChips({ filters, onRemove, total }: FilterChipsProps) {
  const chips: Chip[] = [];

  if (filters.city) {
    chips.push({
      id: "city",
      label: filters.city,
      onRemove: () => onRemove("city"),
    });
  }

  if (filters.check_in && filters.check_out) {
    const ci = parseISO(filters.check_in);
    const co = parseISO(filters.check_out);
    chips.push({
      id: "dates",
      label: `${format(ci, "MMM d")} – ${format(co, "MMM d")}`,
      onRemove: () => onRemove("check_in"),
    });
  }

  if (filters.price_min != null || filters.price_max != null) {
    const min = filters.price_min ?? 0;
    const max = filters.price_max ?? 800;
    const cs = currencySymbol(filters.city);
    chips.push({
      id: "price",
      label: `${cs}${min} – ${cs}${max}`,
      onRemove: () => onRemove("price_min"),
    });
  }

  if (filters.min_rating != null) {
    chips.push({
      id: "rating",
      label: `${filters.min_rating}+ ★`,
      onRemove: () => onRemove("min_rating"),
    });
  }

  if (filters.sort && filters.sort !== "popularity") {
    chips.push({
      id: "sort",
      label: SORT_LABELS[filters.sort] ?? filters.sort,
      onRemove: () => onRemove("sort"),
    });
  }

  (filters.property_types ?? []).forEach((pt) => {
    chips.push({
      id: `pt:${pt}`,
      label: PROPERTY_TYPE_LABELS[pt] ?? pt,
      onRemove: () => onRemove({ pt }),
    });
  });

  (filters.amenities ?? []).forEach((am) => {
    chips.push({
      id: `am:${am}`,
      label: AMENITY_LABELS[am] ?? am,
      onRemove: () => onRemove({ amenity: am }),
      highlight: true,
    });
  });

  if (chips.length === 0) {
    return (
      <div className="text-sm text-gray-500">
        {total != null ? (
          <span>
            <span className="font-semibold text-gray-800">{total.toLocaleString()}</span>{" "}
            {filters.city ? `places in ${filters.city}` : "places found"}
          </span>
        ) : null}
      </div>
    );
  }

  return (
    <div className="flex items-center gap-2 flex-wrap">
      {total != null && (
        <span className="text-sm text-gray-500 mr-1">
          <span className="font-semibold text-gray-800">{total.toLocaleString()}</span> found
        </span>
      )}
      {chips.map((chip) => (
        <button
          key={chip.id}
          onClick={chip.onRemove}
          className={`inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-medium border transition-all ${
            chip.highlight
              ? "bg-[#fff0f3] border-[#fca5a5] text-[#c41840] hover:bg-[#ffe0e8]"
              : "bg-gray-50 border-gray-200 text-gray-700 hover:bg-gray-100 hover:border-gray-300"
          }`}
        >
          {chip.label}
          <span className="text-current opacity-60 text-sm leading-none">×</span>
        </button>
      ))}
      {chips.length > 1 && (
        <button
          onClick={() => onRemove("all" as keyof SearchFilters)}
          className="text-xs text-gray-400 underline underline-offset-2 hover:text-gray-600 ml-1"
        >
          Clear all
        </button>
      )}
    </div>
  );
}
