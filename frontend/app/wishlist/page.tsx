"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import Image from "next/image";
import { getListing, ListingDetail } from "@/lib/api";
import { useWishlist, useCompare } from "@/app/providers";
import { StarRating } from "@/components/ui/StarRating";
import { CompareBar } from "@/components/compare/CompareBar";
import { ListingCard } from "@/lib/api";

export default function WishlistPage() {
  const { wishlist, toggle } = useWishlist();
  const { toggle: toggleCompare, has: inCompare } = useCompare();
  const [listings, setListings] = useState<ListingDetail[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (wishlist.length === 0) {
      setListings([]);
      setLoading(false);
      return;
    }
    setLoading(true);
    Promise.all(wishlist.map((id) => getListing(id)))
      .then((results) => {
        setListings(results);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, [wishlist]);

  return (
    <div className="min-h-screen bg-gray-50 pb-20">
      <nav className="bg-white border-b border-gray-100 px-6 py-3 flex items-center gap-4 sticky top-0 z-20">
        <Link
          href="/"
          className="flex items-center gap-1.5 text-sm font-medium text-gray-600 hover:text-gray-900"
        >
          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
          </svg>
          Search
        </Link>
        <Link href="/" className="flex items-center gap-1.5 ml-2">
          <div className="w-6 h-6 bg-[#e61e4d] rounded-lg flex items-center justify-center">
            <svg className="w-3 h-3 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3.055 11H5a2 2 0 012 2v1a2 2 0 002 2 2 2 0 012 2v2.945M8 3.935V5.5A2.5 2.5 0 0010.5 8h.5a2 2 0 012 2 2 2 0 104 0 2 2 0 012-2h1.064M15 20.488V18a2 2 0 012-2h3.064" />
            </svg>
          </div>
          <span className="text-sm font-bold text-gray-900">Travel<span className="text-[#e61e4d]">AI</span></span>
        </Link>
      </nav>

      <main className="max-w-6xl mx-auto px-6 pt-8">
        <div className="flex items-center gap-3 mb-6">
          <h1 className="text-2xl font-bold text-gray-900">Saved places</h1>
          <span className="text-sm bg-gray-100 text-gray-500 px-2.5 py-0.5 rounded-full font-medium">
            {wishlist.length}
          </span>
        </div>

        {loading ? (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="rounded-2xl overflow-hidden border border-gray-100 bg-white">
                <div className="aspect-[4/3] skeleton" />
                <div className="p-4 space-y-2">
                  <div className="skeleton h-4 w-3/4" />
                  <div className="skeleton h-3 w-1/2" />
                </div>
              </div>
            ))}
          </div>
        ) : wishlist.length === 0 ? (
          <div className="text-center py-24">
            <div className="w-20 h-20 bg-gray-100 rounded-full flex items-center justify-center mx-auto mb-4">
              <svg className="w-10 h-10 text-gray-300" viewBox="0 0 24 24" fill="currentColor">
                <path d="M21 8.25c0-2.485-2.099-4.5-4.688-4.5-1.935 0-3.597 1.126-4.312 2.733-.715-1.607-2.377-2.733-4.313-2.733C5.1 3.75 3 5.765 3 8.25c0 7.22 9 12 9 12s9-4.78 9-12z" />
              </svg>
            </div>
            <h2 className="text-xl font-semibold text-gray-700 mb-2">No saved places yet</h2>
            <p className="text-sm text-gray-400 mb-6">
              Tap the heart on any listing to save it here
            </p>
            <Link
              href="/"
              className="px-6 py-3 bg-[#e61e4d] text-white text-sm font-semibold rounded-xl hover:bg-[#c41840] transition-colors"
            >
              Start exploring
            </Link>
          </div>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {listings.map((listing) => {
              const card: ListingCard = {
                id: listing.id,
                name: listing.name,
                type: listing.type,
                city: listing.city,
                neighbourhood: listing.neighbourhood,
                lat: listing.lat,
                lng: listing.lng,
                price_per_night: listing.base_price,
                rating: listing.rating,
                review_count: listing.review_count,
                key_amenities: listing.amenities.slice(0, 4),
                photo: listing.photos[0],
              };

              return (
                <div
                  key={listing.id}
                  className="bg-white rounded-2xl overflow-hidden border border-gray-100 shadow-sm hover:shadow-md transition-shadow"
                >
                  <Link href={`/listings/${listing.id}`} className="block relative aspect-[4/3]">
                    {listing.photos[0] && (
                      <Image
                        src={listing.photos[0]}
                        alt={listing.name}
                        fill
                        className="object-cover"
                        sizes="(max-width: 768px) 100vw, 33vw"
                      />
                    )}
                    <button
                      onClick={(e) => {
                        e.preventDefault();
                        toggle(listing.id);
                      }}
                      className="absolute top-3 right-3 w-8 h-8 rounded-full bg-white/90 flex items-center justify-center shadow-sm"
                    >
                      <svg className="w-4 h-4 text-[#e61e4d] fill-current" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M21 8.25c0-2.485-2.099-4.5-4.688-4.5-1.935 0-3.597 1.126-4.312 2.733-.715-1.607-2.377-2.733-4.313-2.733C5.1 3.75 3 5.765 3 8.25c0 7.22 9 12 9 12s9-4.78 9-12z" />
                      </svg>
                    </button>
                  </Link>
                  <div className="p-4">
                    <div className="flex items-start justify-between gap-2 mb-1">
                      <div>
                        <p className="text-xs text-gray-400">{listing.neighbourhood ?? listing.city}</p>
                        <h3 className="text-sm font-semibold text-gray-900 mt-0.5">{listing.name}</h3>
                      </div>
                      {listing.rating != null && (
                        <StarRating rating={listing.rating} count={listing.review_count} />
                      )}
                    </div>
                    <p className="text-base font-bold text-gray-900 mt-2">
                      ${Math.round(listing.base_price)}<span className="text-xs font-normal text-gray-500"> / night</span>
                    </p>
                    <div className="flex gap-2 mt-3">
                      <Link
                        href={`/listings/${listing.id}`}
                        className="flex-1 text-center py-1.5 text-xs font-semibold border border-gray-900 text-gray-900 rounded-lg hover:bg-gray-900 hover:text-white transition-colors"
                      >
                        View
                      </Link>
                      <button
                        onClick={() => toggleCompare(card)}
                        className={`flex-1 py-1.5 text-xs font-semibold rounded-lg border transition-colors ${
                          inCompare(listing.id)
                            ? "bg-gray-900 text-white border-gray-900"
                            : "border-gray-200 text-gray-600 hover:border-gray-400"
                        }`}
                      >
                        {inCompare(listing.id) ? "Comparing" : "Compare"}
                      </button>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </main>

      <CompareBar />
    </div>
  );
}
