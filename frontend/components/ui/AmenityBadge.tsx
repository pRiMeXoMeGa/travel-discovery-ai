"use client";

import { AMENITY_LABELS } from "@/lib/search-state";

const AMENITY_ICONS: Record<string, string> = {
  wifi: "⚡",
  pool: "🏊",
  kitchen: "🍳",
  parking: "🚗",
  balcony: "🌿",
  ac: "❄️",
  gym: "💪",
  washer: "🫧",
  pets_allowed: "🐾",
  hot_tub: "🛁",
  bbq: "🔥",
  workspace: "💻",
  beach_access: "🏖️",
  concierge: "🛎️",
  breakfast_included: "☕",
  ev_charger: "🔋",
  elevator: "🛗",
  baby_cot: "🍼",
};

interface AmenityBadgeProps {
  amenity: string;
  variant?: "chip" | "icon";
}

export function AmenityBadge({ amenity, variant = "chip" }: AmenityBadgeProps) {
  const label = AMENITY_LABELS[amenity] ?? amenity;
  const icon = AMENITY_ICONS[amenity] ?? "✓";

  if (variant === "icon") {
    return (
      <div className="flex items-center gap-2 text-sm text-gray-700">
        <span className="text-base w-5 text-center">{icon}</span>
        <span>{label}</span>
      </div>
    );
  }

  return (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-gray-100 text-gray-600">
      {icon} {label}
    </span>
  );
}
