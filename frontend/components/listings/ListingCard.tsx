"use client";

import Image from "next/image";
import Link from "next/link";
import { memo, useCallback } from "react";
import { ListingCard as ListingCardType } from "@/lib/api";
import { StarRating } from "@/components/ui/StarRating";
import { AMENITY_LABELS } from "@/lib/search-state";
import { price } from "@/lib/currency";
import { useWishlist, useCompare, useHover, useIsHovered } from "@/app/providers";

interface ListingCardProps {
  listing: ListingCardType;
  searchParams?: string;
}

const TYPE_LABELS: Record<string, string> = {
  "entire place": "Entire place",
  "private room": "Private room",
  hotel: "Hotel",
  "shared room": "Shared room",
};

// Memoised so that HoverContext updates (setHoveredId on any card) only
// re-render the two cards whose highlight state actually changes — not all
// 20 cards simultaneously, which was creating a render storm that starved
// the browser's main thread and delayed click/navigation events.
export const ListingCard = memo(function ListingCard({ listing, searchParams }: ListingCardProps) {
  const { has: inWishlist, toggle: toggleWL } = useWishlist();
  const { has: inCompare, toggle: toggleCmp } = useCompare();
  // useIsHovered only re-renders this card when ITS OWN highlight state flips.
  // Previously we read hoveredId from HoverContext and computed `=== listing.id`
  // here, which caused all 20 cards to re-render on every mouse-enter/leave.
  const isHighlighted = useIsHovered(listing.id);
  // setHoveredId comes from the stable HoverSetContext via useHover — only used
  // for writing, so consuming useHover() here is fine (MapView reads hoveredId
  // from the same context but cards now only need the setter).
  const { setHoveredId } = useHover();

  const saved = inWishlist(listing.id);
  const comparing = inCompare(listing.id);

  const href = `/listings/${listing.id}${searchParams ? `?${searchParams}` : ""}`;

  const handleMouseEnter = useCallback(() => setHoveredId(listing.id), [listing.id, setHoveredId]);
  const handleMouseLeave = useCallback(() => setHoveredId(null), [setHoveredId]);

  return (
    <article
      className={`group relative bg-white rounded-2xl overflow-hidden border transition-all duration-200 cursor-pointer ${
        isHighlighted
          ? "border-gray-900 shadow-lg ring-2 ring-gray-900 ring-offset-0"
          : "border-gray-100 hover:border-gray-200 hover:shadow-md"
      }`}
      onMouseEnter={handleMouseEnter}
      onMouseLeave={handleMouseLeave}
    >
      {/* Photo */}
      <Link href={href} className="block relative aspect-[4/3] bg-gray-100 overflow-hidden">
        {listing.photo ? (
          <Image
            src={listing.photo}
            alt={listing.name}
            fill
            className="object-cover transition-transform duration-300 group-hover:scale-105"
            sizes="(max-width: 768px) 100vw, (max-width: 1200px) 50vw, 33vw"
            loading="lazy"
            decoding="async"
            fetchPriority="low"
          />
        ) : (
          <div className="w-full h-full bg-gray-200 flex items-center justify-center text-gray-400 text-4xl">
            🏠
          </div>
        )}

        {/* Type badge */}
        <span className="absolute top-3 left-3 bg-white/90 backdrop-blur-sm text-xs font-medium px-2.5 py-1 rounded-full text-gray-700 shadow-sm">
          {TYPE_LABELS[listing.type] ?? listing.type}
        </span>

        {/* Wishlist button */}
        <button
          aria-label={saved ? "Remove from wishlist" : "Save to wishlist"}
          onClick={(e) => {
            e.preventDefault();
            toggleWL(listing.id);
          }}
          className="absolute top-3 right-3 w-8 h-8 rounded-full bg-white/90 backdrop-blur-sm flex items-center justify-center shadow-sm hover:bg-white transition-colors"
        >
          <svg
            className={`w-4 h-4 transition-colors ${saved ? "text-[#e61e4d] fill-current" : "text-gray-500 hover:text-[#e61e4d]"}`}
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={1.5}
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M21 8.25c0-2.485-2.099-4.5-4.688-4.5-1.935 0-3.597 1.126-4.312 2.733-.715-1.607-2.377-2.733-4.313-2.733C5.1 3.75 3 5.765 3 8.25c0 7.22 9 12 9 12s9-4.78 9-12z"
            />
          </svg>
        </button>

        {/* Distance badge */}
        {listing.distance_km != null && (
          <span className="absolute bottom-3 left-3 bg-black/60 text-white text-xs px-2 py-0.5 rounded-full">
            {listing.distance_km.toFixed(1)} km away
          </span>
        )}
      </Link>

      {/* Content */}
      <Link href={href} className="block p-3.5">
        <div className="flex items-start justify-between gap-2 mb-1">
          <div className="flex-1 min-w-0">
            <p className="text-xs text-gray-400 truncate">
              {listing.neighbourhood ?? listing.city}
            </p>
            <h2 className="text-sm font-semibold text-gray-900 truncate mt-0.5 leading-snug">
              {listing.name}
            </h2>
          </div>
          {listing.rating != null && (
            <div className="flex-shrink-0">
              <StarRating rating={listing.rating} count={listing.review_count} />
            </div>
          )}
        </div>

        {/* Amenities */}
        {listing.key_amenities.length > 0 && (
          <div className="flex gap-1 flex-wrap mt-2 mb-2.5">
            {listing.key_amenities.slice(0, 3).map((am) => (
              <span
                key={am}
                className="text-xs px-1.5 py-0.5 bg-gray-50 text-gray-500 rounded"
              >
                {AMENITY_LABELS[am] ?? am}
              </span>
            ))}
          </div>
        )}

        {/* Price */}
        <div className="flex items-baseline justify-between mt-auto">
          <div>
            <span className="text-base font-bold text-gray-900">
              {price(listing.price_per_night, listing.city)}
            </span>
            <span className="text-xs text-gray-500 ml-1">/ night</span>
          </div>
          {listing.total_for_stay != null && (
            <span className="text-xs text-gray-500">
              {price(listing.total_for_stay, listing.city)} total
            </span>
          )}
        </div>
      </Link>

      {/* Compare toggle */}
      <div className="px-3.5 pb-3">
        <button
          onClick={() => toggleCmp(listing)}
          className={`w-full text-xs py-1.5 rounded-lg border font-medium transition-all ${
            comparing
              ? "bg-gray-900 text-white border-gray-900"
              : "border-gray-200 text-gray-500 hover:border-gray-400 hover:text-gray-700"
          }`}
        >
          {comparing ? "✓ Comparing" : "+ Compare"}
        </button>
      </div>
    </article>
  );
});

// Skeleton version
export function ListingCardSkeleton() {
  return (
    <div className="bg-white rounded-2xl overflow-hidden border border-gray-100">
      <div className="aspect-[4/3] skeleton" />
      <div className="p-3.5 space-y-2">
        <div className="skeleton h-3 w-2/3" />
        <div className="skeleton h-4 w-full" />
        <div className="flex gap-1">
          <div className="skeleton h-4 w-12" />
          <div className="skeleton h-4 w-12" />
          <div className="skeleton h-4 w-14" />
        </div>
        <div className="skeleton h-5 w-24 mt-2" />
      </div>
    </div>
  );
}
