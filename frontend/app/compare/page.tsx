"use client";

import { useEffect, useState, Suspense } from "react";
import { useSearchParams, useRouter } from "next/navigation";
import Link from "next/link";
import Image from "next/image";
import { compareListing, ListingDetail } from "@/lib/api";
import { AMENITY_LABELS } from "@/lib/search-state";
import { StarRating } from "@/components/ui/StarRating";
import { useWishlist } from "@/app/providers";

const ALL_AMENITIES = Object.keys(AMENITY_LABELS);

function CompareCell({
  value,
  highlight,
}: {
  value: React.ReactNode;
  highlight?: boolean;
}) {
  return (
    <td
      className={`px-4 py-3 text-sm text-center border-b border-gray-50 ${
        highlight ? "bg-[#fff0f3] font-semibold text-[#c41840]" : "text-gray-700"
      }`}
    >
      {value}
    </td>
  );
}

function ComparePageInner() {
  const params = useSearchParams();
  const router = useRouter();
  const { toggle: toggleWL, has: inWishlist } = useWishlist();

  const ids = (params.get("ids") ?? "").split(",").filter(Boolean);

  const [data, setData] = useState<{ listings: ListingDetail[] } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (ids.length < 2) {
      setLoading(false);
      return;
    }
    setLoading(true);
    compareListing(ids)
      .then((res) => {
        setData(res);
        setLoading(false);
      })
      .catch((err) => {
        setError(err.message);
        setLoading(false);
      });
  }, [ids.join(",")]); // eslint-disable-line react-hooks/exhaustive-deps

  const listings = data?.listings ?? [];

  // Find best values for highlighting
  const bestPrice = listings.length
    ? Math.min(...listings.map((l) => l.base_price))
    : null;
  const bestRating = listings.length
    ? Math.max(...listings.map((l) => l.rating ?? 0))
    : null;
  const bestReviews = listings.length
    ? Math.max(...listings.map((l) => l.review_count))
    : null;

  return (
    <div className="min-h-screen bg-gray-50 pb-12">
      <nav className="bg-white border-b border-gray-100 px-6 py-3 flex items-center gap-4 sticky top-0 z-20">
        <button
          onClick={() => router.back()}
          className="flex items-center gap-1.5 text-sm font-medium text-gray-600 hover:text-gray-900"
        >
          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
          </svg>
          Back
        </button>
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
          <h1 className="text-2xl font-bold text-gray-900">Compare places</h1>
          {listings.length > 0 && (
            <span className="text-sm text-gray-400">{listings.length} properties</span>
          )}
        </div>

        {loading ? (
          <div className="text-center py-24">
            <div className="w-8 h-8 border-2 border-gray-300 border-t-gray-700 rounded-full animate-spin mx-auto" />
          </div>
        ) : error ? (
          <div className="text-center py-24 text-gray-500">{error}</div>
        ) : ids.length < 2 ? (
          <div className="text-center py-24">
            <div className="text-4xl mb-4">📋</div>
            <h2 className="text-xl font-semibold text-gray-700 mb-2">
              Select at least 2 places to compare
            </h2>
            <Link href="/" className="text-[#e61e4d] underline text-sm">
              Browse listings
            </Link>
          </div>
        ) : (
          <div className="bg-white rounded-2xl border border-gray-100 overflow-hidden shadow-sm">
            <div className="overflow-x-auto">
              <table className="w-full">
                <thead>
                  <tr className="border-b border-gray-100">
                    <th className="px-4 py-4 text-left text-xs font-semibold text-gray-400 uppercase tracking-wider w-36">
                      Property
                    </th>
                    {listings.map((l) => (
                      <th key={l.id} className="px-4 py-4 min-w-[200px]">
                        <Link href={`/listings/${l.id}`} className="block">
                          <div className="relative h-28 rounded-xl overflow-hidden mb-3">
                            <Image
                              src={l.photos[0] ?? ""}
                              alt={l.name}
                              fill
                              className="object-cover"
                              sizes="200px"
                            />
                          </div>
                          <p className="text-sm font-semibold text-gray-900 text-left line-clamp-2">
                            {l.name}
                          </p>
                          <p className="text-xs text-gray-400 text-left mt-0.5">
                            {l.neighbourhood ?? l.city}
                          </p>
                        </Link>
                        <div className="flex gap-1.5 mt-2">
                          <Link
                            href={`/listings/${l.id}`}
                            className="flex-1 text-center py-1 text-xs font-semibold bg-gray-900 text-white rounded-lg hover:bg-gray-800 transition-colors"
                          >
                            View
                          </Link>
                          <button
                            onClick={() => toggleWL(l.id)}
                            className={`flex-1 py-1 text-xs font-semibold rounded-lg border transition-colors ${
                              inWishlist(l.id)
                                ? "bg-[#e61e4d] text-white border-[#e61e4d]"
                                : "border-gray-200 text-gray-600 hover:border-gray-400"
                            }`}
                          >
                            {inWishlist(l.id) ? "Saved" : "Save"}
                          </button>
                        </div>
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {/* Price */}
                  <tr>
                    <td className="px-4 py-3 text-xs font-semibold text-gray-500 uppercase tracking-wider bg-gray-50">
                      Price / night
                    </td>
                    {listings.map((l) => (
                      <CompareCell
                        key={l.id}
                        value={`$${Math.round(l.base_price)}`}
                        highlight={l.base_price === bestPrice}
                      />
                    ))}
                  </tr>

                  {/* Rating */}
                  <tr>
                    <td className="px-4 py-3 text-xs font-semibold text-gray-500 uppercase tracking-wider bg-gray-50">
                      Rating
                    </td>
                    {listings.map((l) => (
                      <CompareCell
                        key={l.id}
                        value={
                          l.rating != null ? (
                            <StarRating rating={l.rating} count={l.review_count} />
                          ) : (
                            <span className="text-gray-300">No rating</span>
                          )
                        }
                        highlight={l.rating === bestRating}
                      />
                    ))}
                  </tr>

                  {/* Reviews */}
                  <tr>
                    <td className="px-4 py-3 text-xs font-semibold text-gray-500 uppercase tracking-wider bg-gray-50">
                      Reviews
                    </td>
                    {listings.map((l) => (
                      <CompareCell
                        key={l.id}
                        value={`${l.review_count.toLocaleString()} reviews`}
                        highlight={l.review_count === bestReviews}
                      />
                    ))}
                  </tr>

                  {/* Type */}
                  <tr>
                    <td className="px-4 py-3 text-xs font-semibold text-gray-500 uppercase tracking-wider bg-gray-50">
                      Type
                    </td>
                    {listings.map((l) => (
                      <CompareCell key={l.id} value={l.type} />
                    ))}
                  </tr>

                  {/* Beds */}
                  <tr>
                    <td className="px-4 py-3 text-xs font-semibold text-gray-500 uppercase tracking-wider bg-gray-50">
                      Beds
                    </td>
                    {listings.map((l) => (
                      <CompareCell key={l.id} value={`${l.beds} bed${l.beds !== 1 ? "s" : ""}`} />
                    ))}
                  </tr>

                  {/* Amenities header */}
                  <tr>
                    <td
                      colSpan={listings.length + 1}
                      className="px-4 py-2 bg-gray-900 text-white text-xs font-semibold uppercase tracking-wider"
                    >
                      Amenities
                    </td>
                  </tr>

                  {/* Amenity rows — only show amenities present in at least one listing */}
                  {ALL_AMENITIES.filter((am) =>
                    listings.some((l) => l.amenities.includes(am))
                  ).map((am) => (
                    <tr key={am}>
                      <td className="px-4 py-2.5 text-xs text-gray-600 bg-gray-50">
                        {AMENITY_LABELS[am]}
                      </td>
                      {listings.map((l) => {
                        const has = l.amenities.includes(am);
                        return (
                          <td
                            key={l.id}
                            className="px-4 py-2.5 text-center border-b border-gray-50"
                          >
                            {has ? (
                              <span className="text-green-500 text-base">✓</span>
                            ) : (
                              <span className="text-gray-200 text-base">—</span>
                            )}
                          </td>
                        );
                      })}
                    </tr>
                  ))}

                  {/* AI Verdict placeholder */}
                  <tr>
                    <td className="px-4 py-3 text-xs font-semibold text-gray-500 uppercase tracking-wider bg-gray-50">
                      AI Verdict
                    </td>
                    {listings.map((l) => (
                      <td
                        key={l.id}
                        className="px-4 py-3 text-center border-b border-gray-50"
                      >
                        <div className="inline-flex items-center gap-1.5 px-2 py-1 bg-[#fff0f3] text-[#e61e4d] text-xs rounded-lg">
                          <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
                          </svg>
                          Coming in Phase 5
                        </div>
                      </td>
                    ))}
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}

export default function ComparePage() {
  return (
    <Suspense>
      <ComparePageInner />
    </Suspense>
  );
}
