"use client";

import { useEffect, useState, useCallback } from "react";
import { useParams, useSearchParams, useRouter } from "next/navigation";
import Image from "next/image";
import Link from "next/link";
import {
  getListing,
  getReviews,
  ListingDetail,
  Review,
  ReviewsResponse,
  AvailabilityDay,
} from "@/lib/api";
import { price } from "@/lib/currency";
import { StarRating } from "@/components/ui/StarRating";
import { AmenityBadge } from "@/components/ui/AmenityBadge";
import { useWishlist, useCompare } from "@/app/providers";
import { format, parseISO, differenceInDays } from "date-fns";
import dynamic from "next/dynamic";

// Import map only on client
const MiniMap = dynamic(() => import("@/components/map/MiniMap"), { ssr: false });

// ---- Gallery ----
function Gallery({ photos, name }: { photos: string[]; name: string }) {
  const [lightbox, setLightbox] = useState<number | null>(null);
  const displayed = photos.slice(0, 5);

  return (
    <>
      <div className="relative">
        <div className="grid grid-cols-4 grid-rows-2 gap-2 h-[420px] rounded-2xl overflow-hidden">
          {/* Hero */}
          <div className="col-span-2 row-span-2 relative cursor-pointer" onClick={() => setLightbox(0)}>
            <Image
              src={displayed[0] ?? "/placeholder.jpg"}
              alt={name}
              fill
              className="object-cover hover:brightness-95 transition-all"
              sizes="50vw"
              priority
            />
          </div>
          {/* Side grid */}
          {displayed.slice(1, 5).map((src, idx) => (
            <div
              key={idx}
              className="relative cursor-pointer"
              onClick={() => setLightbox(idx + 1)}
            >
              <Image
                src={src}
                alt={`${name} photo ${idx + 2}`}
                fill
                className="object-cover hover:brightness-95 transition-all"
                sizes="25vw"
              />
            </div>
          ))}
        </div>

        {photos.length > 5 && (
          <button
            onClick={() => setLightbox(0)}
            className="absolute bottom-4 right-4 bg-white/90 backdrop-blur-sm px-4 py-2 rounded-xl text-sm font-semibold shadow-sm border border-gray-200 hover:bg-white transition-colors"
          >
            Show all {photos.length} photos
          </button>
        )}
      </div>

      {/* Lightbox */}
      {lightbox !== null && (
        <div
          className="fixed inset-0 bg-black/90 z-50 flex items-center justify-center"
          onClick={() => setLightbox(null)}
        >
          <button
            className="absolute top-4 right-4 text-white text-3xl hover:opacity-70"
            onClick={() => setLightbox(null)}
          >
            ×
          </button>
          <button
            className="absolute left-4 text-white text-4xl hover:opacity-70"
            onClick={(e) => { e.stopPropagation(); setLightbox((l) => Math.max(0, (l ?? 0) - 1)); }}
          >
            ‹
          </button>
          <div className="relative w-[90vw] max-w-4xl h-[80vh]" onClick={(e) => e.stopPropagation()}>
            <Image
              src={photos[lightbox]}
              alt={`${name} ${lightbox + 1}`}
              fill
              className="object-contain"
              sizes="90vw"
            />
          </div>
          <button
            className="absolute right-4 text-white text-4xl hover:opacity-70"
            onClick={(e) => { e.stopPropagation(); setLightbox((l) => Math.min(photos.length - 1, (l ?? 0) + 1)); }}
          >
            ›
          </button>
          <div className="absolute bottom-4 text-white text-sm">
            {lightbox + 1} / {photos.length}
          </div>
        </div>
      )}
    </>
  );
}

// ---- Availability Calendar ----
function AvailabilityCalendar({
  window: avail,
  onSelect,
  checkIn,
  checkOut,
  city,
}: {
  window: AvailabilityDay[];
  onSelect: (checkIn: string, checkOut: string) => void;
  checkIn: string | null;
  checkOut: string | null;
  city?: string;
}) {
  const [selecting, setSelecting] = useState<string | null>(null);

  const handleClick = (day: AvailabilityDay) => {
    if (!day.available) return;
    if (!selecting) {
      setSelecting(day.date);
    } else {
      if (day.date > selecting) {
        onSelect(selecting, day.date);
        setSelecting(null);
      } else {
        setSelecting(day.date);
      }
    }
  };

  // Group by week rows
  const firstDate = avail[0] ? parseISO(avail[0].date) : new Date();
  const startPad = firstDate.getDay();

  return (
    <div>
      <div className="grid grid-cols-7 gap-1 text-center">
        {["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"].map((d) => (
          <div key={d} className="text-xs text-gray-400 font-medium py-1">{d}</div>
        ))}
        {Array(startPad).fill(null).map((_, i) => <div key={`pad-${i}`} />)}
        {avail.map((day) => {
          const isSelected =
            day.date === checkIn || day.date === checkOut;
          const isInRange =
            checkIn && checkOut && day.date > checkIn && day.date < checkOut;
          const isSelecting = selecting && day.date === selecting;

          let cls =
            "h-10 flex flex-col items-center justify-center rounded-xl text-xs transition-all ";
          if (!day.available) {
            cls += "text-gray-300 line-through cursor-not-allowed";
          } else if (isSelected || isSelecting) {
            cls += "bg-gray-900 text-white font-semibold cursor-pointer";
          } else if (isInRange) {
            cls += "bg-gray-100 text-gray-800 cursor-pointer";
          } else {
            cls += "hover:bg-gray-100 cursor-pointer text-gray-700";
          }

          return (
            <div
              key={day.date}
              className={cls}
              onClick={() => handleClick(day)}
              title={day.available ? `${price(day.price, city)}/night` : "Unavailable"}
            >
              <span>{parseISO(day.date).getDate()}</span>
              {day.available && (
                <span className="text-[9px] opacity-60 leading-none">
                  {price(day.price, city)}
                </span>
              )}
            </div>
          );
        })}
      </div>
      {selecting && (
        <p className="text-sm text-center text-gray-500 mt-3">
          Select your check-out date
        </p>
      )}
    </div>
  );
}

// ---- Reviews ----
function ReviewsSection({ listingId }: { listingId: string }) {
  const [reviews, setReviews] = useState<ReviewsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(1);
  const [language, setLanguage] = useState("");
  const [minScore, setMinScore] = useState<number | undefined>();

  useEffect(() => {
    setLoading(true);
    getReviews(listingId, { language: language || undefined, min_score: minScore, page })
      .then((r) => { setReviews(r); setLoading(false); })
      .catch(() => setLoading(false));
  }, [listingId, language, minScore, page]);

  const aspects = ["cleanliness", "location", "value", "noise", "staff"] as const;

  const aspectLabel = (k: string) => k.charAt(0).toUpperCase() + k.slice(1);
  const sentimentColor = (s: number | null | undefined) => {
    if (s == null) return "bg-gray-100 text-gray-500";
    if (s > 0) return "bg-green-50 text-green-700";
    if (s < 0) return "bg-red-50 text-red-600";
    return "bg-gray-100 text-gray-500";
  };

  return (
    <div>
      {/* Filters */}
      <div className="flex flex-wrap items-center gap-2 mb-5">
        <select
          value={language}
          onChange={(e) => { setLanguage(e.target.value); setPage(1); }}
          className="text-sm border border-gray-200 rounded-lg px-3 py-1.5 text-gray-700 bg-white focus:outline-none focus:border-gray-400"
        >
          <option value="">All languages</option>
          <option value="en">English</option>
          <option value="ar">Arabic</option>
          <option value="fr">French</option>
          <option value="es">Spanish</option>
          <option value="de">German</option>
          <option value="pt">Portuguese</option>
        </select>

        <select
          value={minScore ?? ""}
          onChange={(e) => { setMinScore(e.target.value ? Number(e.target.value) : undefined); setPage(1); }}
          className="text-sm border border-gray-200 rounded-lg px-3 py-1.5 text-gray-700 bg-white focus:outline-none focus:border-gray-400"
        >
          <option value="">Any score</option>
          <option value="3">3+ stars</option>
          <option value="4">4+ stars</option>
          <option value="5">5 stars</option>
        </select>
      </div>

      {loading ? (
        <div className="space-y-4">
          {[1, 2, 3].map((i) => (
            <div key={i} className="p-4 rounded-xl border border-gray-100">
              <div className="flex gap-3 mb-3">
                <div className="skeleton w-10 h-10 rounded-full" />
                <div className="flex-1 space-y-2">
                  <div className="skeleton h-3 w-24" />
                  <div className="skeleton h-3 w-16" />
                </div>
              </div>
              <div className="skeleton h-4 w-full mb-1" />
              <div className="skeleton h-4 w-3/4" />
            </div>
          ))}
        </div>
      ) : !reviews || reviews.results.length === 0 ? (
        <div className="text-center py-12 text-gray-400">
          <div className="text-4xl mb-2">💬</div>
          <p>No reviews match your filters.</p>
        </div>
      ) : (
        <div className="space-y-4">
          {reviews.results.map((review) => (
            <div key={review.id} className="p-4 rounded-xl border border-gray-100 hover:border-gray-200 transition-colors">
              <div className="flex items-start justify-between gap-3 mb-2">
                <div className="flex items-center gap-2">
                  <div className="w-9 h-9 rounded-full bg-gradient-to-br from-gray-200 to-gray-300 flex items-center justify-center text-sm font-semibold text-gray-600">
                    {review.reviewer?.charAt(0) ?? "?"}
                  </div>
                  <div>
                    <p className="text-sm font-medium text-gray-800">
                      {review.reviewer ?? "Anonymous"}
                    </p>
                    {review.date && (
                      <p className="text-xs text-gray-400">
                        {format(parseISO(review.date), "MMM yyyy")}
                      </p>
                    )}
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  {review.rating != null && (
                    <StarRating rating={review.rating} size="sm" />
                  )}
                  {review.language && review.language !== "en" && (
                    <span className="text-xs px-1.5 py-0.5 bg-gray-100 text-gray-500 rounded uppercase">
                      {review.language}
                    </span>
                  )}
                </div>
              </div>

              <p className="text-sm text-gray-700 leading-relaxed">{review.text}</p>

              {/* Aspect scores */}
              {review.aspects && Object.values(review.aspects).some((v) => v != null) && (
                <div className="flex flex-wrap gap-1.5 mt-2.5">
                  {aspects.map((aspect) => {
                    const val = review.aspects?.[aspect];
                    if (val == null) return null;
                    return (
                      <span
                        key={aspect}
                        className={`text-xs px-2 py-0.5 rounded-full font-medium ${sentimentColor(val)}`}
                      >
                        {aspectLabel(aspect)}: {val > 0 ? "+" : ""}{val > 0 ? "positive" : "negative"}
                      </span>
                    );
                  })}
                </div>
              )}
            </div>
          ))}

          {reviews.total > 20 && (
            <div className="flex items-center justify-center gap-2 pt-2">
              <button
                disabled={page <= 1}
                onClick={() => setPage((p) => p - 1)}
                className="px-4 py-2 text-sm border border-gray-200 rounded-xl disabled:opacity-40 hover:bg-gray-50"
              >
                Previous
              </button>
              <span className="text-sm text-gray-500">Page {page}</span>
              <button
                disabled={page * 20 >= reviews.total}
                onClick={() => setPage((p) => p + 1)}
                className="px-4 py-2 text-sm border border-gray-200 rounded-xl disabled:opacity-40 hover:bg-gray-50"
              >
                Next
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ---- Price Breakdown Widget ----
function BookingWidget({
  listing,
  checkIn: initialCheckIn,
  checkOut: initialCheckOut,
}: {
  listing: ListingDetail;
  checkIn: string | null;
  checkOut: string | null;
}) {
  const [checkIn, setCheckIn] = useState(initialCheckIn);
  const [checkOut, setCheckOut] = useState(initialCheckOut);
  const [confirmed, setConfirmed] = useState(false);

  const nights =
    checkIn && checkOut
      ? differenceInDays(parseISO(checkOut), parseISO(checkIn))
      : 0;

  const nightly = listing.base_price;
  const subtotal = nights * nightly;
  const cleaningFee = Math.round(nightly * 0.12);
  const serviceFee = Math.round(subtotal * 0.14);
  const taxes = Math.round(subtotal * 0.1);
  const total = subtotal + cleaningFee + serviceFee + taxes;

  if (confirmed) {
    return (
      <div className="bg-white rounded-2xl border border-gray-200 shadow-lg p-6 text-center">
        <div className="w-16 h-16 bg-green-50 rounded-full flex items-center justify-center mx-auto mb-4">
          <svg className="w-8 h-8 text-green-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
          </svg>
        </div>
        <h3 className="text-lg font-bold text-gray-900 mb-1">Reservation confirmed!</h3>
        <p className="text-sm text-gray-500 mb-4">
          Your trip to <span className="font-semibold">{listing.name}</span> is confirmed.
          {checkIn && checkOut && (
            <span> {format(parseISO(checkIn), "MMM d")} – {format(parseISO(checkOut), "MMM d, yyyy")}</span>
          )}
        </p>
        <div className="bg-gray-50 rounded-xl p-3 text-left mb-4">
          <p className="text-xs text-gray-500 mb-1">Booking reference</p>
          <p className="text-sm font-mono font-semibold text-gray-800">
            TDAI-{Math.random().toString(36).slice(2, 8).toUpperCase()}
          </p>
        </div>
        <button
          onClick={() => setConfirmed(false)}
          className="text-sm text-gray-500 underline underline-offset-2"
        >
          Book again
        </button>
      </div>
    );
  }

  return (
    <div className="bg-white rounded-2xl border border-gray-200 shadow-lg p-5">
      <div className="flex items-baseline gap-1 mb-4">
        <span className="text-2xl font-bold text-gray-900">${Math.round(nightly)}</span>
        <span className="text-sm text-gray-500">/ night</span>
      </div>

      {/* Date inputs */}
      <div className="grid grid-cols-2 gap-2 mb-3">
        <div className="border border-gray-200 rounded-xl p-3">
          <p className="text-[10px] text-gray-400 font-semibold uppercase tracking-wider mb-1">Check-in</p>
          <input
            type="date"
            value={checkIn ?? ""}
            onChange={(e) => setCheckIn(e.target.value)}
            min={format(new Date(), "yyyy-MM-dd")}
            className="text-sm font-medium text-gray-800 w-full outline-none"
          />
        </div>
        <div className="border border-gray-200 rounded-xl p-3">
          <p className="text-[10px] text-gray-400 font-semibold uppercase tracking-wider mb-1">Check-out</p>
          <input
            type="date"
            value={checkOut ?? ""}
            onChange={(e) => setCheckOut(e.target.value)}
            min={checkIn ?? format(new Date(), "yyyy-MM-dd")}
            className="text-sm font-medium text-gray-800 w-full outline-none"
          />
        </div>
      </div>

      {/* Price breakdown */}
      {nights > 0 && (
        <div className="border-t border-gray-100 pt-3 mb-4 space-y-2">
          <div className="flex justify-between text-sm">
            <span className="text-gray-600">{price(nightly, listing.city)} × {nights} night{nights !== 1 ? "s" : ""}</span>
            <span className="font-medium">{price(subtotal, listing.city)}</span>
          </div>
          <div className="flex justify-between text-sm">
            <span className="text-gray-600">Cleaning fee</span>
            <span className="font-medium">{price(cleaningFee, listing.city)}</span>
          </div>
          <div className="flex justify-between text-sm">
            <span className="text-gray-600">Service fee</span>
            <span className="font-medium">{price(serviceFee, listing.city)}</span>
          </div>
          <div className="flex justify-between text-sm">
            <span className="text-gray-600">Taxes</span>
            <span className="font-medium">{price(taxes, listing.city)}</span>
          </div>
          <div className="flex justify-between text-sm font-bold text-gray-900 border-t border-gray-100 pt-2">
            <span>Total</span>
            <span>{price(total, listing.city)}</span>
          </div>
        </div>
      )}

      <button
        onClick={() => nights > 0 && setConfirmed(true)}
        disabled={nights === 0}
        className="w-full py-3 bg-[#e61e4d] text-white font-semibold rounded-xl hover:bg-[#c41840] transition-colors disabled:opacity-40 disabled:cursor-not-allowed text-sm"
      >
        {nights > 0 ? `Reserve · ${price(total, listing.city)}` : "Select dates to reserve"}
      </button>

      <p className="text-xs text-center text-gray-400 mt-2">
        No charges yet — this is a demo
      </p>
    </div>
  );
}

// ---- Main detail page ----
export default function ListingDetailPage() {
  const params = useParams();
  const searchParams = useSearchParams();
  const router = useRouter();

  const id = params.id as string;
  const [listing, setListing] = useState<ListingDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const { has: inWishlist, toggle: toggleWL } = useWishlist();
  const { has: inCompare } = useCompare();

  const checkIn = searchParams.get("check_in");
  const checkOut = searchParams.get("check_out");
  const [calCheckIn, setCalCheckIn] = useState<string | null>(checkIn);
  const [calCheckOut, setCalCheckOut] = useState<string | null>(checkOut);

  const backHref = `/?${searchParams.toString()}` || "/";

  useEffect(() => {
    getListing(id)
      .then((l) => { setListing(l); setLoading(false); })
      .catch((err) => { setError(err.message); setLoading(false); });
  }, [id]);

  const handleCalSelect = useCallback((ci: string, co: string) => {
    setCalCheckIn(ci);
    setCalCheckOut(co);
  }, []);

  if (loading) {
    return (
      <div className="min-h-screen bg-white">
        <div className="max-w-6xl mx-auto px-6 pt-6">
          <div className="skeleton h-6 w-32 mb-6" />
          <div className="skeleton aspect-[2/1] w-full rounded-2xl mb-8" />
          <div className="grid grid-cols-3 gap-8">
            <div className="col-span-2 space-y-4">
              <div className="skeleton h-8 w-2/3" />
              <div className="skeleton h-4 w-full" />
              <div className="skeleton h-4 w-4/5" />
            </div>
            <div className="skeleton h-64 rounded-2xl" />
          </div>
        </div>
      </div>
    );
  }

  if (error || !listing) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="text-center">
          <p className="text-gray-500 mb-4">{error ?? "Listing not found"}</p>
          <Link href="/" className="text-[#e61e4d] underline">Back to search</Link>
        </div>
      </div>
    );
  }

  const saved = inWishlist(listing.id);
  const aspects = listing.aspect_avg;
  const hasAspects = aspects && Object.values(aspects).some((v) => v != null);

  const aspectLabel: Record<string, string> = {
    cleanliness: "Cleanliness",
    location: "Location",
    value: "Value",
    noise: "Noise",
    staff: "Staff",
  };

  const aspectColor = (v: number | null | undefined) => {
    if (v == null) return null;
    if (v > 0) return { bar: "bg-green-400", text: "text-green-700" };
    if (v < 0) return { bar: "bg-red-400", text: "text-red-600" };
    return { bar: "bg-gray-300", text: "text-gray-500" };
  };

  return (
    <div className="min-h-screen bg-white pb-16">
      {/* Top nav */}
      <nav className="sticky top-0 z-20 bg-white border-b border-gray-100 px-6 py-3 flex items-center justify-between">
        <Link
          href={backHref}
          className="flex items-center gap-1.5 text-sm font-medium text-gray-700 hover:text-gray-900"
        >
          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
          </svg>
          Back to results
        </Link>

        <div className="flex items-center gap-2">
          <Link href="/" className="flex items-center gap-1.5">
            <div className="w-6 h-6 bg-[#e61e4d] rounded-lg flex items-center justify-center">
              <svg className="w-3 h-3 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3.055 11H5a2 2 0 012 2v1a2 2 0 002 2 2 2 0 012 2v2.945M8 3.935V5.5A2.5 2.5 0 0010.5 8h.5a2 2 0 012 2 2 2 0 104 0 2 2 0 012-2h1.064M15 20.488V18a2 2 0 012-2h3.064" />
              </svg>
            </div>
            <span className="text-sm font-bold text-gray-900">Travel<span className="text-[#e61e4d]">AI</span></span>
          </Link>
        </div>

        <button
          onClick={() => toggleWL(listing.id)}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl border border-gray-200 text-sm font-medium text-gray-600 hover:border-gray-400 transition-colors"
        >
          <svg
            className={`w-4 h-4 ${saved ? "text-[#e61e4d] fill-current" : "text-gray-400"}`}
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={1.5}
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M21 8.25c0-2.485-2.099-4.5-4.688-4.5-1.935 0-3.597 1.126-4.312 2.733-.715-1.607-2.377-2.733-4.313-2.733C5.1 3.75 3 5.765 3 8.25c0 7.22 9 12 9 12s9-4.78 9-12z" />
          </svg>
          {saved ? "Saved" : "Save"}
        </button>
      </nav>

      <main className="max-w-6xl mx-auto px-6 pt-6">
        {/* Breadcrumb */}
        <div className="flex items-center gap-2 text-xs text-gray-400 mb-4">
          <Link href="/" className="hover:text-gray-600">Home</Link>
          <span>/</span>
          <Link href={`/?city=${listing.city}`} className="hover:text-gray-600">{listing.city}</Link>
          {listing.neighbourhood && (
            <>
              <span>/</span>
              <span className="text-gray-500">{listing.neighbourhood}</span>
            </>
          )}
        </div>

        {/* Gallery */}
        <Gallery photos={listing.photos} name={listing.name} />

        {/* Content grid */}
        <div className="grid grid-cols-3 gap-8 mt-8">
          {/* Left column */}
          <div className="col-span-2 space-y-8">
            {/* Title */}
            <div>
              <div className="flex items-start justify-between gap-4">
                <div>
                  <h1 className="text-2xl font-bold text-gray-900 mb-1">{listing.name}</h1>
                  <p className="text-gray-500 text-sm">
                    {listing.type} · {listing.city}
                    {listing.neighbourhood ? `, ${listing.neighbourhood}` : ""}
                    {listing.beds ? ` · ${listing.beds} bed${listing.beds !== 1 ? "s" : ""}` : ""}
                  </p>
                </div>
                {listing.rating != null && (
                  <div className="flex-shrink-0">
                    <StarRating rating={listing.rating} count={listing.review_count} size="md" />
                  </div>
                )}
              </div>

              {/* Host */}
              <div className="flex items-center gap-3 mt-4 pt-4 border-t border-gray-100">
                <div className="w-10 h-10 rounded-full bg-gradient-to-br from-[#e61e4d] to-[#ff7d8b] flex items-center justify-center text-white font-semibold text-sm">
                  {listing.host.name.charAt(0)}
                </div>
                <div>
                  <p className="text-sm font-semibold text-gray-800">
                    Hosted by {listing.host.name}
                    {listing.host.superhost && (
                      <span className="ml-2 text-xs bg-[#fff0f3] text-[#e61e4d] px-2 py-0.5 rounded-full font-medium">
                        Superhost
                      </span>
                    )}
                  </p>
                  <p className="text-xs text-gray-400">Member since {listing.host.joined_year}</p>
                </div>
              </div>
            </div>

            {/* AI Summary */}
            {listing.summary && listing.summary !== "No reviews yet." && (
              <div className="bg-gradient-to-br from-[#fff0f3] to-white rounded-2xl p-5 border border-[#fca5a5]/30">
                <div className="flex items-center gap-2 mb-3">
                  <div className="w-6 h-6 bg-[#e61e4d] rounded-lg flex items-center justify-center">
                    <svg className="w-3 h-3 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
                    </svg>
                  </div>
                  <span className="text-sm font-semibold text-[#e61e4d]">AI Review Summary</span>
                </div>
                <p className="text-sm text-gray-700 leading-relaxed">{listing.summary}</p>

                {hasAspects && (
                  <div className="mt-4 grid grid-cols-2 gap-3">
                    {Object.entries(listing.aspect_avg ?? {}).map(([key, val]) => {
                      if (val == null) return null;
                      const colors = aspectColor(val);
                      return (
                        <div key={key} className="flex items-center gap-2">
                          <div className="flex-1">
                            <div className="flex justify-between mb-1">
                              <span className="text-xs text-gray-600">{aspectLabel[key] ?? key}</span>
                              <span className={`text-xs font-semibold ${colors?.text}`}>
                                {val > 0 ? "Positive" : val < 0 ? "Negative" : "Neutral"}
                              </span>
                            </div>
                            <div className="h-1.5 bg-gray-100 rounded-full overflow-hidden">
                              <div
                                className={`h-full rounded-full ${colors?.bar}`}
                                style={{ width: `${Math.abs(val) * 100}%` }}
                              />
                            </div>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            )}

            {/* Neighbourhood price context */}
            {listing.neighbourhood_price_pct != null && (
              <div className="flex items-center gap-3 p-3 bg-gray-50 rounded-xl text-sm text-gray-600">
                <span className="text-xl">
                  {listing.neighbourhood_price_pct < 0.5 ? "💰" : listing.neighbourhood_price_pct < 0.75 ? "📊" : "💎"}
                </span>
                <span>
                  Priced in the{" "}
                  <span className="font-semibold text-gray-800">
                    {Math.round(listing.neighbourhood_price_pct * 100)}th percentile
                  </span>{" "}
                  for {listing.neighbourhood ?? listing.city}
                </span>
              </div>
            )}

            {/* Amenities */}
            <div>
              <h2 className="text-lg font-bold text-gray-900 mb-4">What this place offers</h2>
              <div className="grid grid-cols-2 gap-3">
                {listing.amenities.map((am) => (
                  <AmenityBadge key={am} amenity={am} variant="icon" />
                ))}
              </div>
            </div>

            {/* Availability calendar */}
            <div>
              <h2 className="text-lg font-bold text-gray-900 mb-1">Availability</h2>
              <p className="text-sm text-gray-500 mb-4">Select your check-in and check-out dates</p>
              <AvailabilityCalendar
                window={listing.availability_window}
                onSelect={handleCalSelect}
                checkIn={calCheckIn}
                checkOut={calCheckOut}
                city={listing.city}
              />
            </div>

            {/* Neighbourhood map */}
            <div>
              <h2 className="text-lg font-bold text-gray-900 mb-1">Where you will be</h2>
              <p className="text-sm text-gray-500 mb-4">
                {listing.neighbourhood ?? listing.city}, {listing.city}
              </p>
              <div className="h-64 rounded-2xl overflow-hidden border border-gray-100">
                <MiniMap lat={listing.lat} lng={listing.lng} name={listing.name} />
              </div>
            </div>

            {/* Reviews */}
            <div>
              <div className="flex items-center gap-3 mb-5">
                <h2 className="text-lg font-bold text-gray-900">Reviews</h2>
                {listing.rating != null && (
                  <StarRating rating={listing.rating} count={listing.review_count} size="md" />
                )}
              </div>
              <ReviewsSection listingId={listing.id} />
            </div>
          </div>

          {/* Right column — sticky booking widget */}
          <div className="col-span-1">
            <div className="sticky top-20">
              <BookingWidget
                listing={listing}
                checkIn={calCheckIn}
                checkOut={calCheckOut}
              />
            </div>
          </div>
        </div>
      </main>
    </div>
  );
}
